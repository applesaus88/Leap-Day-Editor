/* nativemod.c — Leap Day per-individual-enemy tuning, applied in-process.
 *
 * Ships as libnativemod.so, embedded as a DT_NEEDED on libmain.so (see
 * core/nativemod.py). The game's own linker loads it — no root, no Frida. On a
 * background thread it waits for il2cpp to initialise, then every ~400ms
 * enumerates live enemies (liveness API), keys each to its editor placement
 * (chunk basename + col + rowFromBottom), and applies that enemy's tuning:
 * projectile (objectToShoot), health, walk speed.
 *
 * The tuning table lives in config.h, regenerated per build from the project.
 *
 * Design notes / calibration are in the project-native-il2cpp-modloader memory.
 */
#include <android/log.h>
#include <pthread.h>
#include <unistd.h>
#include <stddef.h>
#include <stdint.h>
#include <inttypes.h>
#include <stdlib.h>
#include <stdio.h>
#include <string.h>
#include <math.h>
#include <sys/mman.h>

#define TAG "NATIVEMOD"
#ifdef NATIVEMOD_DEBUG
#define LOG(...) __android_log_print(ANDROID_LOG_INFO, TAG, __VA_ARGS__)
#else
/* release: strip all logcat output; the `if (0)` keeps args referenced so
 * variables used only inside LOG(...) don't warn, and the dead call is removed. */
#define LOG(...) do { if (0) __android_log_print(ANDROID_LOG_INFO, TAG, __VA_ARGS__); } while (0)
#endif

/* ---- runtime tuning table -------------------------------------------------- *
 * The per-mod tuning table used to be baked into this .so at compile time (an old
 * config.h with const g_tunes[]), which meant every tuning change needed the NDK
 * to recompile. Now ONE prebuilt libnativemod.so serves every mod: the build
 * PATCHES the config blob below (finds MAGIC, writes a u32 length + the tuning
 * text) — pure bytes, no compiler. At startup load_config() parses that text into
 * the same g_tunes[]/g_bakes[] the rest of the code reads. See core/nativemod.py.
 *
 * Blob layout:  [16-byte MAGIC][u32 length, little-endian][config text]
 * Text format (one record per line, '|'-separated; projectile "-" = none):
 *   v1
 *   t|chunk|col|row|projectile|health|walk|shootmult|firemult
 *   b|cls|projectile|speed
 * The blob is `volatile` so the compiler can't constant-fold its (zero) build-time
 * contents — the bytes are only known after the build patches them. */
typedef struct {
    const char* chunk;       /* chunk basename ("*" = any) */
    int         col;         /* sx; <= -1000 = any column  */
    int         row;         /* rowFromBottom              */
    const char* projectile;  /* GameObject name, or NULL   */
    int         health;      /* -1 = leave                 */
    float       walk;        /* absolute walk speed, < 0 = leave    */
    float       shootmult;   /* launch-speed multiplier (1 = leave) */
    float       firemult;    /* fire-rate multiplier (1 = leave)    */
    float       walkmult;    /* walk-speed multiplier (1 = leave, 0 = frozen) */
    float       muzzle_x;    /* projectile spawn offset FORWARD (facing dir), units. default 16 */
    float       muzzle_y;    /* projectile spawn offset UP, units. default 6 (raise for big/low projectiles) */
} EnemyTune;
typedef struct {
    const char* cls;         /* enemy class          */
    const char* projectile;  /* GameObject name      */
    float       speed;       /* absolute launch speed */
} ShootBake;
/* respawn link: in chunk `chunk`, a checkpoint should drop the player at cell
 * (col,row) — the 🟢 respawn end of a 🚩flag→🟢respawn connection line. */
typedef struct { const char* chunk; int col, row; } RespawnLink;

/* ghostR cell: a Ghost that spawned at chunk `chunk` cell (col,row) should fly
 * LEFT-to-RIGHT. The editor places "ghostR"; the build emits it as the only
 * game-spawnable ghost ("ghostL", leftToRight=0), and the native mod flips just
 * these instances to leftToRight=1 so they move the other way. */
typedef struct { const char* chunk; int col, row; } GhostRCell;

#define MAX_TUNES 512
#define MAX_BAKES 128
#define MAX_RLINKS 64
#define MAX_GRCELLS 64
static EnemyTune   g_tunes[MAX_TUNES]; static int g_ntunes;
static RespawnLink g_rlinks[MAX_RLINKS]; static int g_nrlinks;
static GhostRCell  g_grcells[MAX_GRCELLS]; static int g_ngrcells;
static ShootBake g_bakes[MAX_BAKES]; static int g_nbakes;

/* axe spin+boomerang tunables (baked defaults; an "x|" config line overrides).
 * range = how far out the axe flies before curving back (world units ~ pixels,
 * tile ~ 16); speed = flight speed; spin = degrees/sec. */
static float g_axe_range = 28.0f;
static float g_axe_speed = 110.0f;
static float g_axe_spin  = 900.0f;
static float g_axe_hang  = 0.25f;   /* pause (seconds) at the far end before returning */

#define CONFIG_CAP 65536
static const unsigned char CONFIG_MAGIC[16] = {
    0x4C,0x44,0x4E,0x4D,0xC0,0xDE,0xF1,0x9E,0x43,0x46,0x47,0x42,0x4C,0x4F,0x42,0x7F };
/* initialised with MAGIC (non-zero) so the whole array lands in .data (in-file),
 * giving the build room to write up to CONFIG_CAP bytes over it. */
__attribute__((used, aligned(16)))
static volatile unsigned char g_config_blob[CONFIG_CAP] = {
    0x4C,0x44,0x4E,0x4D,0xC0,0xDE,0xF1,0x9E,0x43,0x46,0x47,0x42,0x4C,0x4F,0x42,0x7F };
static char g_cfgtext[CONFIG_CAP];   /* mutable copy we tokenise in place */

/* split the next '|'-delimited field of a line in place, advancing *sp */
static char* cfg_field(char** sp) {
    char* s = *sp; if (!s) return NULL;
    char* bar = strchr(s, '|');
    if (bar) { *bar = 0; *sp = bar + 1; } else { *sp = NULL; }
    return s;
}
static const char* cfg_orNull(const char* s) {
    return (s && s[0] == '-' && s[1] == 0) ? NULL : s;   /* "-" = none */
}

/* ---- playtest-feature flags (ported from the Frida musicbg agent) ---------
 * Baked into the build so Build+Install matches Playtest. Gated by the same
 * Settings toggles, serialized as one `p|...` config line. */
static int g_pt_keep = 0;      /* keep music: no side-room muffle/fade + timer on side screens */
static int g_pt_bgbare = 0;    /* bare background: strip theme scenery, keep the sky           */
static int g_pt_smooth = 0;    /* smooth camera (phase 2)                                      */
static int g_pt_locky  = 0;    /* lock camera Y  (phase 2)                                      */
static int g_pt_captop = 1;    /* lock-Y: cap the top too (phase 2)                             */
static int g_pt_hidet  = 0;    /* hide the run timer                                           */
static int g_pt_hidep  = 0;    /* hide the progression bar                                     */
static int g_pt_respawn = 0;   /* respawn flags (phase 3)                                      */
/* parse the patched config blob into g_tunes[]/g_bakes[]. Byte-by-byte volatile
 * reads keep the optimizer from assuming the build-time (empty) contents. */
static void load_config(void) {
    for (int i = 0; i < 16; i++)
        if ((unsigned char)g_config_blob[i] != CONFIG_MAGIC[i]) {
            LOG("config: MAGIC missing — unpatched .so, no tunings"); return;
        }
    uint32_t len = 0;
    for (int i = 0; i < 4; i++)
        len |= ((uint32_t)(unsigned char)g_config_blob[16 + i]) << (8 * i);
    if (len == 0 || len >= CONFIG_CAP - 20) { LOG("config: bad len=%u", len); return; }
    for (uint32_t i = 0; i < len; i++) g_cfgtext[i] = (char)g_config_blob[20 + i];
    g_cfgtext[len] = 0;

    char* p = g_cfgtext;
    while (p && *p) {
        char* line = p;
        char* nl = strchr(p, '\n');
        if (nl) { *nl = 0; p = nl + 1; } else p = NULL;
        if (line[0] == 't' && line[1] == '|') {
            char* r = line + 2;
            char* f_chunk = cfg_field(&r); char* f_col = cfg_field(&r);
            char* f_row = cfg_field(&r);   char* f_proj = cfg_field(&r);
            char* f_hp = cfg_field(&r);    char* f_walk = cfg_field(&r);
            char* f_sm = cfg_field(&r);    char* f_fm = cfg_field(&r);
            char* f_wm = cfg_field(&r);    /* walkmult — absent in old configs -> NULL */
            char* f_mx = cfg_field(&r);    /* muzzle_x/_y — absent in old configs -> NULL (defaults) */
            char* f_my = cfg_field(&r);
            if (f_chunk && f_col && f_row && g_ntunes < MAX_TUNES) {
                EnemyTune* t = &g_tunes[g_ntunes++];
                t->chunk = f_chunk;
                t->col = atoi(f_col);
                t->row = atoi(f_row);
                t->projectile = cfg_orNull(f_proj);
                t->health = f_hp ? atoi(f_hp) : -1;
                t->walk   = f_walk ? (float)atof(f_walk) : -1.0f;
                t->shootmult = f_sm ? (float)atof(f_sm) : 1.0f;
                t->firemult  = f_fm ? (float)atof(f_fm) : 1.0f;
                t->walkmult  = f_wm ? (float)atof(f_wm) : 1.0f;
                t->muzzle_x  = (f_mx && f_mx[0]) ? (float)atof(f_mx) : 16.0f;  /* default forward 16 */
                t->muzzle_y  = (f_my && f_my[0]) ? (float)atof(f_my) : 6.0f;   /* default up 6 */
            }
        } else if (line[0] == 'b' && line[1] == '|') {
            char* r = line + 2;
            char* f_cls = cfg_field(&r); char* f_proj = cfg_field(&r);
            char* f_spd = cfg_field(&r);
            if (f_cls && f_proj && f_spd && g_nbakes < MAX_BAKES) {
                ShootBake* b = &g_bakes[g_nbakes++];
                b->cls = f_cls; b->projectile = f_proj; b->speed = (float)atof(f_spd);
            }
        } else if (line[0] == 'r' && line[1] == '|') {
            /* respawn link: r|chunk_basename|respCol|respRow — a checkpoint in this
             * chunk drops the player at that cell (the 🟢 end of a flag→respawn line) */
            char* r = line + 2;
            char* f_chunk = cfg_field(&r); char* f_col = cfg_field(&r); char* f_row = cfg_field(&r);
            if (f_chunk && f_row && g_nrlinks < MAX_RLINKS) {
                RespawnLink* rl = &g_rlinks[g_nrlinks++];
                rl->chunk = f_chunk;                       /* points into g_cfgtext (kept) */
                rl->col = f_col ? atoi(f_col) : 0;
                rl->row = atoi(f_row);
            }
        } else if (line[0] == 'g' && line[1] == 'r' && line[2] == '|') {
            /* ghostR cell: gr|chunk|col|rowFromBottom — flip this ghost to
             * leftToRight=1 (fly the opposite way from a normal ghostL). */
            char* r = line + 3;
            char* f_chunk = cfg_field(&r); char* f_col = cfg_field(&r); char* f_row = cfg_field(&r);
            if (f_chunk && f_row && g_ngrcells < MAX_GRCELLS) {
                GhostRCell* gc = &g_grcells[g_ngrcells++];
                gc->chunk = f_chunk;                      /* points into g_cfgtext (kept) */
                gc->col = f_col ? atoi(f_col) : 0;
                gc->row = atoi(f_row);
            }
        } else if (line[0] == 'x' && line[1] == '|') {
            /* axe boomerang settings: x|range|speed|spin (blank field = keep default) */
            char* r = line + 2;
            char* f_rng = cfg_field(&r); char* f_spd = cfg_field(&r);
            char* f_spn = cfg_field(&r); char* f_hng = cfg_field(&r);
            if (f_rng && f_rng[0]) g_axe_range = (float)atof(f_rng);   /* blank = keep default */
            if (f_spd && f_spd[0]) g_axe_speed = (float)atof(f_spd);
            if (f_spn && f_spn[0]) g_axe_spin  = (float)atof(f_spn);
            if (f_hng && f_hng[0]) g_axe_hang  = (float)atof(f_hng);
            LOG("config: axe range=%.1f speed=%.1f spin=%.1f hang=%.2f",
                (double)g_axe_range, (double)g_axe_speed, (double)g_axe_spin, (double)g_axe_hang);
        } else if (line[0] == 'p' && line[1] == '|') {
            /* playtest features (gated by their Settings toggles):
             * p|keep|bgbare|smooth|locky|captop|hidetimer|hideprog|respawn */
            char* r = line + 2;
            char* a = cfg_field(&r); char* b = cfg_field(&r); char* c = cfg_field(&r);
            char* d = cfg_field(&r); char* e = cfg_field(&r); char* f = cfg_field(&r);
            char* g = cfg_field(&r); char* h = cfg_field(&r);
            g_pt_keep   = a ? atoi(a) : 0;
            g_pt_bgbare = b ? atoi(b) : 0;
            g_pt_smooth = c ? atoi(c) : 0;
            g_pt_locky  = d ? atoi(d) : 0;
            g_pt_captop = e ? atoi(e) : 1;
            g_pt_hidet  = f ? atoi(f) : 0;
            g_pt_hidep  = g ? atoi(g) : 0;
            g_pt_respawn = h ? atoi(h) : 0;
            LOG("config: playtest keep=%d bgbare=%d smooth=%d locky=%d captop=%d hidet=%d hidep=%d respawn=%d",
                g_pt_keep, g_pt_bgbare, g_pt_smooth, g_pt_locky, g_pt_captop, g_pt_hidet, g_pt_hidep, g_pt_respawn);
        }
        /* "v1" version line and anything else ignored */
    }
    LOG("config: loaded %d tuning(s), %d bake(s), %d respawn-link(s)", g_ntunes, g_nbakes, g_nrlinks);
}

/* ---- il2cpp C API (opaque handles) --------------------------------------- */
typedef void Il2CppDomain; typedef void Il2CppThread; typedef void Il2CppAssembly;
typedef void Il2CppImage; typedef void Il2CppClass; typedef void Il2CppObject;
typedef void Il2CppType; typedef void FieldInfo; typedef void MethodInfo;
typedef void Il2CppString; typedef void Il2CppArray;

extern Il2CppDomain*          il2cpp_domain_get(void);
extern Il2CppThread*          il2cpp_thread_attach(Il2CppDomain*);
extern const Il2CppAssembly** il2cpp_domain_get_assemblies(const Il2CppDomain*, size_t*);
extern const Il2CppImage*     il2cpp_assembly_get_image(const Il2CppAssembly*);
extern Il2CppClass*           il2cpp_class_from_name(const Il2CppImage*, const char*, const char*);
extern FieldInfo*             il2cpp_class_get_field_from_name(Il2CppClass*, const char*);
extern size_t                 il2cpp_field_get_offset(FieldInfo*);
extern FieldInfo*             il2cpp_class_get_fields(Il2CppClass*, void** iter);
extern const char*            il2cpp_field_get_name(FieldInfo*);
extern const Il2CppType*      il2cpp_field_get_type(FieldInfo*);
extern char*                  il2cpp_type_get_name(const Il2CppType*);
extern Il2CppClass*           il2cpp_object_get_class(Il2CppObject*);
extern const char*            il2cpp_class_get_name(Il2CppClass*);
extern int                    il2cpp_class_is_subclass_of(Il2CppClass*, Il2CppClass*, int);
extern const Il2CppType*      il2cpp_class_get_type(Il2CppClass*);
extern Il2CppObject*          il2cpp_type_get_object(const Il2CppType*);
extern const MethodInfo*      il2cpp_class_get_method_from_name(Il2CppClass*, const char*, int argc);
extern Il2CppObject*          il2cpp_runtime_invoke(const MethodInfo*, void* obj, void** params, Il2CppObject** exc);
extern void                   il2cpp_stop_gc_world(void);
extern void                   il2cpp_start_gc_world(void);
extern void                   il2cpp_field_static_get_value(FieldInfo*, void*);
typedef void (*liveness_cb)(void** objects, int count, void* user);
typedef void* (*liveness_realloc)(void* handle, size_t size, void* user);
extern void*  il2cpp_unity_liveness_allocate_struct(Il2CppClass*, int, liveness_cb, void*, liveness_realloc);
extern void   il2cpp_unity_liveness_calculation_from_statics(void*);
extern void   il2cpp_unity_liveness_finalize(void*);
extern void   il2cpp_unity_liveness_free_struct(void*);

/* ---- globals ------------------------------------------------------------- */
static Il2CppDomain* g_dom;

/* Every enemy family we can tune. NOT every enemy is an EnemyBase: the trunkies
 * (WoolyTrunky / BigWoolyTrunky) are standalone MonoBehaviours with their OWN
 * fields, so we enumerate them as their own classes. Field offsets differ across
 * classes (WoolyTrunky.Snowball@0x70 vs BigWoolyTrunky.Snowball@0x68), so we
 * resolve each field BY NAME per class at init.
 *
 * Liveness matches GC descriptors of CONCRETE classes, so we list concrete
 * classes (abstract EnemyBase enumerates to nothing); subclasses are included.
 *
 * f_parent is the field holding the enemy's chunk GameObject. When NULL the enemy
 * is a transform-child of its chunk and we read transform.parent instead — that's
 * how the trunky MonoBehaviours reference their chunk. */
typedef struct {
    const char* cls;
    const char* f_proj;      /* GameObject projectile field, or NULL */
    const char* f_walk;      /* float walk-speed field, or NULL     */
    const char* f_health;    /* int health field, or NULL           */
    const char* f_parent;    /* chunk GameObject field, or NULL     */
    const char* f_shotspeed; /* float launch-speed field, or NULL   */
    const char* f_firerate;  /* float fire-rate/cooldown field, or NULL */
    Il2CppClass* klass;
    size_t o_proj, o_walk, o_health, o_parent, o_shotspeed, o_firerate;
    int last_n;              /* last live count, for change-triggered heartbeat */
} TuneClass;

/* f_firerate is a cooldown/interval field (EnemyBase.spawnTimer = spawner cadence;
 * Cupid.pauseTime = between shots). f_shotspeed is a launch-speed field where the
 * enemy exposes one (trunkies/Cupid/asteroid); the universal projectile-velocity
 * scaler (below) covers the shooters that don't. */
static TuneClass g_tc[] = {
    {"EnemyWalking",   "objectToShoot",        "velocity", "health", "parentChunkObject", NULL,           "spawnTimer"},
    {"EnemyFlying",    "objectToShoot",        NULL,       "health", "parentChunkObject", NULL,           "spawnTimer"},
    {"EnemyStatic",    "objectToShoot",        NULL,       "health", "parentChunkObject", NULL,           "spawnTimer"},
    {"EnemyJumping",   "objectToShoot",        NULL,       "health", "parentChunkObject", NULL,           "spawnTimer"},
    {"EnemyOnPath",    "objectToShoot",        NULL,       "health", "parentChunkObject", NULL,           "spawnTimer"},
    {"WoolyTrunky",    "Snowball",             "Speed",    NULL,     NULL,                "SnowballSpeed", NULL},
    {"BigWoolyTrunky", "Snowball",             "Speed",    NULL,     NULL,                "SnowballSpeed", NULL},
    {"Cupid",          "Arrow",                NULL,       NULL,     NULL,                "arrowSpeed",    "pauseTime"},
    {"Asteroid",       "mediumAsteroidPrefab", NULL,       NULL,     NULL,                "velocity",      NULL},
    /* Fieldless own-class shooters (no launch-speed field): the universal
     * velocity scaler covers their shots. Enumerated here so they can be tuned at
     * all; field names come from the editor's _SHOOTERS table. Offsets resolve by
     * name per class, so a name absent on a class is simply skipped. */
    {"totemBoomeranger",      "axe",            NULL, "health", NULL, NULL, NULL},
    {"GiantCrab",             "BulletPF",       NULL, "health", NULL, NULL, NULL},
    {"EnemyAnimSkullSoldier", "arrowPrefab",    NULL, "health", NULL, NULL, NULL},
    {"MotherBlob",            "smallBlopPrefab",NULL, "health", NULL, NULL, NULL},
    {"Worm",                  "fly",            NULL, "health", NULL, NULL, NULL},
};
#define NTC (int)(sizeof(g_tc)/sizeof(g_tc[0]))

/* A homing projectile isn't launched ballistically — it's spawned and seeks. */
static int is_homing(const char* p) {
    return p && (strcmp(p, "HomingMissile") == 0 || strcmp(p, "HomingGhost") == 0);
}

/* ---- small helpers ------------------------------------------------------- */
static Il2CppClass* find_class(const char* ns, const char* name) {
    size_t n = 0; const Il2CppAssembly** a = il2cpp_domain_get_assemblies(g_dom, &n);
    for (size_t i = 0; i < n; i++) {
        Il2CppClass* k = il2cpp_class_from_name(il2cpp_assembly_get_image(a[i]), ns, name);
        if (k) return k;
    }
    return NULL;
}
/* Resources.FindObjectsOfTypeAll is NOT reentrant across threads — the worker
 * (enemy/rigidbody enum) and the pump (projectile/collider scan) both call it, so
 * two concurrent calls corrupt Unity's object walk -> SIGSEGV. Serialize every call
 * behind one lock. FindObjectsOfTypeAll returns a fresh GC array each time, so we
 * only need to guard the invoke itself, not the later array read. */
static pthread_mutex_t g_findlock = PTHREAD_MUTEX_INITIALIZER;
static Il2CppArray* find_all_locked(const MethodInfo* m, void** args) {
    if (!m) return NULL;
    pthread_mutex_lock(&g_findlock);
    Il2CppArray* a = il2cpp_runtime_invoke(m, NULL, args, NULL);
    pthread_mutex_unlock(&g_findlock);
    return a;
}
static size_t field_off(Il2CppClass* k, const char* fld) {
    if (!k) return (size_t)-1;
    FieldInfo* f = il2cpp_class_get_field_from_name(k, fld);
    return f ? il2cpp_field_get_offset(f) : (size_t)-1;
}

static Il2CppObject* invoke0(Il2CppObject* obj, const char* method) {
    if (!obj) return NULL;
    const MethodInfo* m = il2cpp_class_get_method_from_name(il2cpp_object_get_class(obj), method, 0);
    return m ? il2cpp_runtime_invoke(m, obj, NULL, NULL) : NULL;
}
static void read_cs_string(Il2CppString* s, char* out, int cap) {
    out[0] = 0; if (!s) return;
    int len = *(int32_t*)((char*)s + 0x10);
    const uint16_t* ch = (const uint16_t*)((char*)s + 0x14);
    int n = len < cap - 1 ? len : cap - 1;
    for (int i = 0; i < n; i++) out[i] = (char)(ch[i] & 0x7F);
    out[n] = 0;
}
static void obj_name(Il2CppObject* o, char* out, int cap) {
    out[0] = 0;
    read_cs_string((Il2CppString*)invoke0(o, "get_name"), out, cap);
}
/* world position via the object's own Transform (runtime_invoke boxes Vector3) */
static int obj_position(Il2CppObject* o, float* xyz) {
    Il2CppObject* tr = invoke0(o, "get_transform"); if (!tr) return 0;
    Il2CppObject* boxed = invoke0(tr, "get_position"); if (!boxed) return 0;
    float* v = (float*)((char*)boxed + 0x10);
    xyz[0]=v[0]; xyz[1]=v[1]; xyz[2]=v[2]; return 1;
}
/* set world position: Transform.set_position(Vector3) — a 12-byte value type is
 * passed to runtime_invoke as a pointer to its three floats. */
static int obj_set_position(Il2CppObject* o, float x, float y, float z) {
    Il2CppObject* tr = invoke0(o, "get_transform"); if (!tr) return 0;
    const MethodInfo* m = il2cpp_class_get_method_from_name(
        il2cpp_object_get_class(tr), "set_position", 1);
    if (!m) return 0;
    float v[3] = { x, y, z };
    void* args[1] = { v };
    il2cpp_runtime_invoke(m, tr, args, NULL);
    return 1;
}
/* A UnityEngine.Object whose native peer has been destroyed still lingers as a
 * managed wrapper (FindObjectsOfTypeAll returns it), but its m_CachedPtr (the
 * IntPtr at 0x10) is null. Invoking get_transform()/get_position() on it derefs
 * that null and crashes at addr 0x0 — so skip anything that isn't alive. */
static int unity_alive(Il2CppObject* o) {
    return o && *(void**)((char*)o + 0x10) != NULL;
}
/* strip a trailing "(Clone)" and take the basename after the last '/' */
static void chunk_basename(const char* full, char* out, int cap) {
    const char* base = full;
    for (const char* p = full; *p; p++) if (*p == '/') base = p + 1;
    int n = 0;
    for (const char* p = base; *p && n < cap - 1; p++, n++) out[n] = *p;
    out[n] = 0;
    /* drop " (Clone)" suffix if present */
    char* c = strstr(out, "(Clone)");
    if (c) { while (c > out && c[-1] == ' ') c--; *c = 0; }
}

/* ---- chunk registry ------------------------------------------------------- */
/* Every placed chunk is a child of a GameObject named "Levels", carrying the
 * chunk's asset name and its bottom-left world origin. The trunky MonoBehaviours
 * are re-parented to a global "SpawnedEnemies" container at spawn, losing their
 * chunk link — so for them we recover the chunk by world position (the chunk
 * whose origin sits just below the enemy). Rebuilt each poll (chunks scroll). */
typedef struct { char name[80]; float ox, oy; } ChunkReg;
static ChunkReg g_chunks[128];
static int      g_nchunks;
static Il2CppObject* g_goType;   /* boxed typeof(GameObject), for FindObjectsOfTypeAll */
/* Resources.FindObjectsOfTypeAll — shared by the chunk registry and enemy enum */
static Il2CppClass*      g_resClass;
static const MethodInfo* g_findAll;

/* A chunk GameObject is named after its asset path ("Levels/...") or "Chunk_N".
 * We find them with FindObjectsOfTypeAll — the SAME call the enemy enumeration
 * uses safely. (Transform.GetChild / GameObject.Find are main-thread-only and
 * hang the game when called from this worker thread — do NOT use them.) */
static int is_chunk_name(const char* s) {
    return strncmp(s, "Levels/", 7) == 0 || strncmp(s, "Chunk_", 6) == 0;
}
static void build_chunk_registry(void) {
    g_nchunks = 0;
    if (!g_findAll) {
        g_resClass = find_class("UnityEngine", "Resources");
        if (g_resClass)
            g_findAll = il2cpp_class_get_method_from_name(g_resClass, "FindObjectsOfTypeAll", 1);
    }
    if (!g_goType) {
        Il2CppClass* goc = find_class("UnityEngine", "GameObject");
        if (goc) g_goType = il2cpp_type_get_object(il2cpp_class_get_type(goc));
    }
    if (!g_findAll || !g_goType) return;
    void* args[1] = { g_goType };
    Il2CppArray* arr = find_all_locked(g_findAll, args);
    if (!arr) return;
    uintptr_t len = *(uintptr_t*)((char*)arr + 0x18);
    void** items  = (void**)((char*)arr + 0x20);
    char full[96];
    for (uintptr_t i = 0; i < len && g_nchunks < 128; i++) {
        Il2CppObject* go = (Il2CppObject*)items[i];
        if (!unity_alive(go)) continue;
        obj_name(go, full, sizeof full);
        if (!is_chunk_name(full)) continue;
        float pos[3]; if (!obj_position(go, pos)) continue;
        chunk_basename(full, g_chunks[g_nchunks].name, sizeof g_chunks[g_nchunks].name);
        g_chunks[g_nchunks].ox = pos[0];
        g_chunks[g_nchunks].oy = pos[1];
        g_nchunks++;
    }
}
/* chunk containing world Y: greatest origin still at or below the enemy */
static const ChunkReg* chunk_for_pos(float wy) {
    const ChunkReg* best = NULL; float bestoy = -1e30f;
    for (int i = 0; i < g_nchunks; i++)
        if (g_chunks[i].oy <= wy + 4.0f && g_chunks[i].oy > bestoy) {
            bestoy = g_chunks[i].oy; best = &g_chunks[i];
        }
    return best;
}

/* (chunk basename, col, row) for an enemy — from its parentChunkObject field, or
 * for a reparented family via the world-position chunk registry. 0 on failure. */
static int enemy_cell(Il2CppObject* e, const TuneClass* tc, char* base, int cap,
                      int* col, int* row) {
    uint8_t* p = (uint8_t*)e;
    float ew[3]; if (!obj_position(e, ew)) return 0;
    float ox, oy;
    if (tc->o_parent != (size_t)-1) {
        Il2CppObject* pc = *(Il2CppObject**)(p + tc->o_parent);
        if (!unity_alive(pc)) return 0;
        char full[96]; obj_name(pc, full, sizeof full); chunk_basename(full, base, cap);
        float cp[3]; if (!obj_position(pc, cp)) return 0;
        ox = cp[0]; oy = cp[1];
    } else {
        const ChunkReg* cr = chunk_for_pos(ew[1]);
        if (!cr) return 0;
        strncpy(base, cr->name, cap - 1); base[cap - 1] = 0;
        ox = cr->ox; oy = cr->oy;
    }
    *col = (int)lroundf((ew[0] - ox - 8.0f) / 16.0f);
    *row = (int)lroundf((ew[1] - oy - 8.0f) / 16.0f);
    return 1;
}

/* ---- projectile prefab cache (name -> GameObject*) ----------------------- */
/* Lazily built once via Resources.FindObjectsOfTypeAll(typeof(GameObject)); we
 * only keep the prefabs whose names appear in the tuning table. */
typedef struct { char name[64]; void* go; } ProjEntry;
static ProjEntry g_proj[64];
static int g_nproj;
static int g_proj_built;

static int projectile_wanted(const char* name) {
    for (int i = 0; i < g_ntunes; i++)
        if (g_tunes[i].projectile && strcmp(g_tunes[i].projectile, name) == 0) return 1;
    return 0;
}
static void build_projectile_cache(void) {
    g_proj_built = 1;
    Il2CppClass* goClass  = find_class("UnityEngine", "GameObject");
    Il2CppClass* resClass = find_class("UnityEngine", "Resources");
    if (!goClass || !resClass) { LOG("proj cache: GameObject/Resources not found"); return; }
    Il2CppObject* goType = il2cpp_type_get_object(il2cpp_class_get_type(goClass));
    const MethodInfo* m = il2cpp_class_get_method_from_name(resClass, "FindObjectsOfTypeAll", 1);
    if (!goType || !m) { LOG("proj cache: FindObjectsOfTypeAll not found"); return; }
    void* args[1] = { goType };
    Il2CppArray* arr = find_all_locked(m, args);
    if (!arr) { LOG("proj cache: query returned null"); return; }
    uintptr_t len = *(uintptr_t*)((char*)arr + 0x18);
    void** items  = (void**)((char*)arr + 0x20);
    char nm[64];
    for (uintptr_t i = 0; i < len && g_nproj < (int)(sizeof(g_proj)/sizeof(g_proj[0])); i++) {
        void* go = items[i];
        if (!go) continue;
        obj_name(go, nm, sizeof nm);
        if (!nm[0] || !projectile_wanted(nm)) continue;
        /* keep the first match for each name */
        int dup = 0;
        for (int j = 0; j < g_nproj; j++) if (strcmp(g_proj[j].name, nm) == 0) { dup = 1; break; }
        if (dup) continue;
        strncpy(g_proj[g_nproj].name, nm, sizeof g_proj[g_nproj].name - 1);
        g_proj[g_nproj].go = go;
        g_nproj++;
    }
    LOG("proj cache built: %d wanted prefab(s) found among %lu GameObjects",
        g_nproj, (unsigned long)len);
#ifdef NATIVEMOD_DEBUG
    for (int i = 0; i < g_ntunes; i++) {
        if (!g_tunes[i].projectile) continue;
        int found = 0;
        for (int j = 0; j < g_nproj; j++) if (!strcmp(g_proj[j].name, g_tunes[i].projectile)) found = 1;
        LOG("proj wanted '%s': %s", g_tunes[i].projectile, found ? "FOUND" : "NOT LOADED");
    }
#endif
}
static void* resolve_projectile(const char* name) {
    if (!g_proj_built) build_projectile_cache();
    for (int i = 0; i < g_nproj; i++) if (strcmp(g_proj[i].name, name) == 0) return g_proj[i].go;
    return NULL;
}

/* ---- instance enumeration ------------------------------------------------- */
/* Resources.FindObjectsOfTypeAll(type) returns EVERY loaded instance of a type,
 * scene objects included — unlike the liveness API, which only reaches objects
 * kept alive from static roots and misses these enemies entirely (they showed
 * on screen yet enumerated to 0). Same call the projectile cache relies on. */
#define MAXE 512
static Il2CppObject* g_found[MAXE]; static int g_count;

/* ---- live-object snapshots: enumerate ONLY on the main thread ---------------
 * FindObjectsOfTypeAll walks Unity's global object registry. The MAIN thread
 * mutates that registry during scene load/teardown, so calling it from the
 * WORKER races destruction and jumps a freed vtable (SIGSEGV at a tiny addr —
 * observed crash: enumerate -> libunity -> pc=0x100). Fix: the PUMP (main
 * thread, where the call is legal) takes the snapshots round-robin; the worker
 * just copies the latest pointer list. No Unity enumeration ever runs off the
 * main thread again. Pointers can be up to a few frames stale, but unity_alive
 * already filters destroyed objects before any field is touched. */
static int resolve_findall(void) {
    if (g_findAll) return 1;
    if (!g_resClass) g_resClass = find_class("UnityEngine", "Resources");
    if (g_resClass) g_findAll = il2cpp_class_get_method_from_name(g_resClass, "FindObjectsOfTypeAll", 1);
    return g_findAll != NULL;
}
#define NSNAP 24
typedef struct { Il2CppClass* k; Il2CppObject* buf[MAXE]; int n; int valid; } Snap;
static Snap g_snap[NSNAP]; static int g_nsnap;
static pthread_mutex_t g_snaplock = PTHREAD_MUTEX_INITIALIZER;
/* register (or find) the snapshot slot for a class; safe from any thread. */
static int snap_slot(Il2CppClass* k) {
    if (!k) return -1;
    pthread_mutex_lock(&g_snaplock);
    int slot = -1;
    for (int i = 0; i < g_nsnap; i++) if (g_snap[i].k == k) { slot = i; break; }
    if (slot < 0 && g_nsnap < NSNAP) { slot = g_nsnap++;
        g_snap[slot].k = k; g_snap[slot].n = 0; g_snap[slot].valid = 0; }
    pthread_mutex_unlock(&g_snaplock);
    return slot;
}
/* MAIN thread: refresh ONE class's snapshot per call (round-robin over all
 * registered classes). Cheap — one FindObjectsOfTypeAll per pump frame. */
static int g_snap_rr;
static void pump_refresh_snapshots(void) {
    if (g_nsnap == 0 || !resolve_findall()) return;
    int i = g_snap_rr % g_nsnap; g_snap_rr++;
    Il2CppClass* k = g_snap[i].k;
    if (!k) return;
    Il2CppObject* ty = il2cpp_type_get_object(il2cpp_class_get_type(k));
    if (!ty) return;
    void* args[1] = { ty };
    Il2CppArray* arr = find_all_locked(g_findAll, args);   /* safe: main thread */
    if (!arr) return;
    uintptr_t len = *(uintptr_t*)((char*)arr + 0x18);
    void** items  = (void**)((char*)arr + 0x20);
    pthread_mutex_lock(&g_snaplock);
    int m = 0;
    for (uintptr_t j = 0; j < len && m < MAXE; j++)
        if (items[j]) g_snap[i].buf[m++] = (Il2CppObject*)items[j];
    g_snap[i].n = m; g_snap[i].valid = 1;
    pthread_mutex_unlock(&g_snaplock);
}
/* WORKER: read the pump's latest snapshot for class k into g_found (no Unity call). */
static int enumerate(Il2CppClass* k) {
    g_count = 0;
    int slot = snap_slot(k);   /* registers on first use; pump fills it next frames */
    if (slot < 0) return 0;
    pthread_mutex_lock(&g_snaplock);
    if (g_snap[slot].valid) {
        int m = g_snap[slot].n;
        for (int i = 0; i < m && g_count < MAXE; i++) g_found[g_count++] = g_snap[slot].buf[i];
    }
    pthread_mutex_unlock(&g_snaplock);
    return g_count;
}

/* ---- apply one enemy ----------------------------------------------------- */
/* Enemies whose launch speed we've already set (so we set it exactly once, not
 * every poll — repeated multiplying would drive it to zero). Cleared per section. */
static Il2CppObject* g_shotfix[512]; static int g_nshotfix;
static int shotfix_seen(Il2CppObject* e) {
    for (int i = 0; i < g_nshotfix; i++) if (g_shotfix[i] == e) return 1;
    return 0;
}
/* dev-baked baseline launch speed for a (class, projectile) combo, or -1 */
static float baked_speed(const char* cls, const char* proj) {
    if (!proj) return -1.0f;
    for (int i = 0; i < g_nbakes; i++)
        if (strcmp(g_bakes[i].cls, cls) == 0 && strcmp(g_bakes[i].projectile, proj) == 0)
            return g_bakes[i].speed;
    return -1.0f;
}

static void apply_tune(Il2CppObject* e, const EnemyTune* t, const TuneClass* tc) {
    uint8_t* p = (uint8_t*)e;
    if (t->health >= 0 && tc->o_health != (size_t)-1)
        *(int32_t*)(p + tc->o_health) = t->health;
    if (t->walk >= 0 && tc->o_walk != (size_t)-1)
        *(float*)(p + tc->o_walk) = t->walk;
    if (t->projectile && tc->o_proj != (size_t)-1) {
        /* Swap the enemy's projectile prefab. For a homing missile this makes the
         * enemy SPAWN the missile on its own cadence/facing — but the missile is
         * born with no cannon (_hmc null) and won't seek; the main-thread pump
         * "adopts" each such orphan via setUp() (see the homing section below). */
        void* proj = resolve_projectile(t->projectile);
#ifdef NATIVEMOD_DEBUG
        void* old = *(void**)(p + tc->o_proj);
#endif
        if (proj) *(void**)(p + tc->o_proj) = proj;
#ifdef NATIVEMOD_DEBUG
        char on[64] = "?"; if (old) obj_name((Il2CppObject*)old, on, sizeof on);
        LOG("proj set '%s' on %s: resolved=%p old=%p(%s)",
            t->projectile, tc->cls, proj, old, on);
#endif
    }
    /* Launch speed + fire rate — applied ONCE per instance (repeated scaling each
     * poll would compound the field to zero/infinity). Independent of the projectile
     * swap, so a shooter can keep its own projectile and just fire faster/slower. */
    if (!shotfix_seen(e)) {
        int did = 0;
        if (t->walkmult != 1.0f && tc->o_walk != (size_t)-1) {
            /* walk-speed multiplier: scale the enemy's own walk speed once. 0 =
             * frozen. Applied once (like the shoot/fire mults) so it doesn't
             * compound each poll. */
            float* wp = (float*)(p + tc->o_walk);
            float nv = *wp * t->walkmult;
            LOG("walkspeed: %.2f -> %.2f on %s (x%.2f)",
                (double)*wp, (double)nv, tc->cls, (double)t->walkmult);
            *wp = nv; did = 1;
        }
        if (tc->o_shotspeed != (size_t)-1) {
            /* shoot speed: dev-baked baseline for this (class, projectile) combo if
             * one exists, then this placement's multiplier. mult==0 is a real OFF —
             * the projectile launches at 0 speed (doesn't move). */
            float mult  = (t->shootmult >= 0.0f) ? t->shootmult : 1.0f;
            float baked = baked_speed(tc->cls, t->projectile);
            if (baked >= 0.0f || mult != 1.0f) {
                float* sp = (float*)(p + tc->o_shotspeed);
                float nv  = ((baked >= 0.0f) ? baked : *sp) * mult;
                LOG("shootspeed: %.1f -> %.1f on %s (baked=%.1f mult=%.2f)",
                    (double)*sp, (double)nv, tc->cls, (double)baked, (double)mult);
                *sp = nv; did = 1;
            }
        }
        if (did && g_nshotfix < 512) g_shotfix[g_nshotfix++] = e;
    }
    /* Fire rate — CONTINUOUS, not once. o_firerate is the LIVE countdown the game
     * RESETS to the full interval (e.g. 5s) after every shot, so dividing it a
     * single time only speeds the FIRST shot and then it reverts to normal. To get
     * a real machine-gun we clamp the countdown low on EVERY pump pass: it can
     * never wind back up to the full reload, so the enemy fires continuously.
     * mult==0 is a real OFF (interval pushed to never). */
    if (tc->o_firerate != (size_t)-1) {
        float fm = t->firemult;
        float* fr = (float*)(p + tc->o_firerate);
        if (fm == 0.0f) {
            *fr = 1.0e9f;                      /* never fires */
        } else if (fm > 1.0f) {
            /* target interval: ~a few seconds base divided by the multiplier, so
             * x100 -> ~0.03s (machine-gun) while a small x2 still just shortens it.
             * Clamp DOWN only, so we never delay a shot that's already due. */
            float cap = 3.0f / fm;
            if (cap < 0.02f) cap = 0.02f;      /* floor so it can't overwhelm/crash */
            if (*fr > cap) *fr = cap;
        }
    }
}

/* Per-instance match cache. A WALKING enemy leaves its spawn cell, so we can only
 * key it to its editor placement right after it spawns. We match each instance
 * ONCE at first sighting (near spawn) and re-apply that tuning as it moves. The
 * cache is cleared whenever no enemies are live (between sections), and a cache
 * hit re-checks the chunk name so a GC-reused pointer can't inherit a stale tune. */
#define CACHE_MAX 2048
static void*            g_seen[CACHE_MAX];
static const EnemyTune* g_seen_tune[CACHE_MAX];
static int              g_seen_n;
static int seen_index(void* inst) {
    for (int i = 0; i < g_seen_n; i++) if (g_seen[i] == inst) return i;
    return -1;
}

/* Best tuning record for (chunk, col, row), by priority rank (lower = better):
 *   chunk-specific exact cell (0-2)  <  chunk-specific col-wildcard (100)
 *   <  any-chunk exact cell (200+)   <  any-chunk col-wildcard (300).
 * chunk "*" matches any chunk; col <= -1000 matches any cell in the chunk. */
static const EnemyTune* match_tune(const char* chunk, int col, int row) {
    const EnemyTune* best = NULL; int bestrank = 1 << 30;
    for (int i = 0; i < g_ntunes; i++) {
        const EnemyTune* t = &g_tunes[i];
        int anychunk = (t->chunk[0] == '*' && t->chunk[1] == 0);
        if (!anychunk && strcmp(t->chunk, chunk) != 0) continue;
        int rank;
        if (t->col <= -1000) {
            rank = anychunk ? 300 : 100;              /* col-wildcard */
        } else {
            int dc = t->col - col, dr = t->row - row, d = dc*dc + dr*dr;
            if (d > 2) continue;                      /* exact cell: within 1 cell diagonally */
            rank = anychunk ? 200 + d : d;
        }
        if (rank < bestrank) { bestrank = rank; best = t; }
    }
    return best;
}

#ifdef NATIVEMOD_DEBUG
/* one-shot: scan ALL loaded GameObjects and report which known projectiles exist,
   to confirm whether a projectile prefab is loadable at runtime in this level. */
static int g_scan_done;
static void debug_scan_projectiles(void) {
    static const char* NAMES[] = {"Fireball","Snowball","big_snowball","Coconut","bomb",
        "Bullet","Arrow","axe","HomingMissile","HomingGhost","SmallBlob",
        "GiantCrabFishBulletAnimated","bird","fly","AcidBall","MudBall","TurtleSpike",
        "ManholeMonsterShot","AsteroidMedium","AsteroidSmall","KingBullet","BlobBall","Ball",
        "homingcannonDown","homingcannonUp","HomingMissileCannon"};   /* probe: is a cannon spawner loaded? */
    Il2CppClass* goClass  = find_class("UnityEngine", "GameObject");
    Il2CppClass* resClass = find_class("UnityEngine", "Resources");
    if (!goClass || !resClass) { LOG("scan: no GameObject/Resources"); return; }
    Il2CppObject* goType = il2cpp_type_get_object(il2cpp_class_get_type(goClass));
    const MethodInfo* m = il2cpp_class_get_method_from_name(resClass, "FindObjectsOfTypeAll", 1);
    void* args[1] = { goType };
    Il2CppArray* arr = find_all_locked(m, args);
    if (!arr) { LOG("scan: query null"); return; }
    uintptr_t len = *(uintptr_t*)((char*)arr + 0x18);
    void** items  = (void**)((char*)arr + 0x20);
    int nn = sizeof(NAMES)/sizeof(NAMES[0]);
    int found[32] = {0}; char nm[64];
    for (uintptr_t i = 0; i < len; i++) {
        if (!items[i]) continue;
        obj_name((Il2CppObject*)items[i], nm, sizeof nm);
        for (int k = 0; k < nn; k++) if (!found[k] && !strcmp(nm, NAMES[k])) found[k] = 1;
    }
    char buf[512]; int off = 0;
    for (int k = 0; k < nn; k++) if (found[k]) off += snprintf(buf+off, sizeof buf-off, "%s ", NAMES[k]);
    LOG("scan: %lu GameObjects loaded; projectiles present: %s", (unsigned long)len, off?buf:"(NONE)");
}
#endif

/* ---- homing missile support ---------------------------------------------- */
/* HomingMissile is spawner-driven: a HomingMissileCannon Instantiates it and
 * calls setUp(cannon, dir), after which it seeks on its own. A trunky instead
 * launches its Snowball prefab ballistically, so a swapped-in missile is never
 * setUp (_hmc stays null) and never seeks. We first OBSERVE what a trunky-spawned
 * missile looks like (state/_hmc), then decide whether zeroing launch speed is
 * enough or the full setUp path is needed. */
static int g_want_homing;
static void homing_init(void) {
    for (int i = 0; i < g_ntunes; i++) if (is_homing(g_tunes[i].projectile)) g_want_homing = 1;
    LOG("homing: want=%d", g_want_homing);
}

/* ---- main-thread pump (inline hook) -------------------------------------- *
 * Scene objects can only be created / reparented on Unity's scripting main
 * thread — doing it from this worker thread corrupts Unity and crashes (proven).
 * So we can't attach a homing cannon from here directly. Instead we inline-hook
 * a per-frame method the game runs ON the main thread (a MonoBehaviour's Update /
 * LateUpdate on a singleton that's always live during play). The hook detours
 * that method through pump_detour(): it runs the game's original body via a
 * trampoline, then calls main_thread_tick() — OUR code, now legally on the main
 * thread. The worker only DETECTS homing enemies and queues jobs; the pump drains
 * the queue and does the actual Instantiate/SetParent.
 *
 * The aarch64 inline hook overwrites the target's first 16 bytes (4 instructions)
 * with an absolute jump to our detour, saving the originals into an executable
 * trampoline that jumps back to target+16. We only hook a target whose first 4
 * instructions are position-INDEPENDENT (no ADR/ADRP/B/BL/B.cond/CBZ/TBZ/literal-
 * LDR): copying those verbatim needs no relocation. MonoBehaviour Update prologues
 * are almost always STP/SUB/MOV/STR (safe); if a candidate isn't, we try the next.
 */

/* executable range of libil2cpp.so, to sanity-check method code pointers */
static uintptr_t g_il_lo, g_il_hi;
static void find_il2cpp_range(void) {
    FILE* f = fopen("/proc/self/maps", "r");
    if (!f) return;
    char line[512];
    while (fgets(line, sizeof line, f)) {
        if (strstr(line, "libil2cpp.so") && strstr(line, " r-xp ")) {
            uintptr_t lo, hi;
            if (sscanf(line, "%" SCNxPTR "-%" SCNxPTR, &lo, &hi) == 2) { g_il_lo = lo; g_il_hi = hi; break; }
        }
    }
    fclose(f);
}

/* reject a prologue we'd have to relocate (any PC-relative op in the first 4) */
static int hook_prologue_safe(const uint32_t* p) {
    for (int i = 0; i < 4; i++) {
        uint32_t x = p[i];
        if ((x & 0x9F000000) == 0x10000000) return 0;   /* ADR       */
        if ((x & 0x9F000000) == 0x90000000) return 0;   /* ADRP      */
        if ((x & 0xFC000000) == 0x14000000) return 0;   /* B         */
        if ((x & 0xFC000000) == 0x94000000) return 0;   /* BL        */
        if ((x & 0xFF000010) == 0x54000000) return 0;   /* B.cond    */
        if ((x & 0x7E000000) == 0x34000000) return 0;   /* CBZ/CBNZ  */
        if ((x & 0x7E000000) == 0x36000000) return 0;   /* TBZ/TBNZ  */
        if ((x & 0x3B000000) == 0x18000000) return 0;   /* LDR-lit/PRFM */
    }
    return 1;
}

/* Make [addr,addr+len) writable+executable for patching. Returns the mode that
 * worked (1=RWX, 2=RW, 0=failed) so the caller/logcat learns what the device
 * allows — W^X policy may forbid RWX on file-backed .text. */
static int make_writable(void* addr, size_t len) {
    long ps = sysconf(_SC_PAGESIZE);
    uintptr_t a = (uintptr_t)addr, s = a & ~(uintptr_t)(ps - 1);
    size_t span = (a + len) - s;
    if (mprotect((void*)s, span, PROT_READ | PROT_WRITE | PROT_EXEC) == 0) return 1;
    if (mprotect((void*)s, span, PROT_READ | PROT_WRITE) == 0) return 2;
    return 0;
}
static void restore_exec(void* addr, size_t len) {
    long ps = sysconf(_SC_PAGESIZE);
    uintptr_t a = (uintptr_t)addr, s = a & ~(uintptr_t)(ps - 1);
    size_t span = (a + len) - s;
    mprotect((void*)s, span, PROT_READ | PROT_EXEC);
}

/* write a 16-byte absolute jump (LDR X17,#8 ; BR X17 ; .quad dst) at *dst4 */
static void emit_abs_jump(uint32_t* dst4, uintptr_t dst) {
    dst4[0] = 0x58000051;   /* LDR X17, #8 */
    dst4[1] = 0xD61F0220;   /* BR  X17     */
    memcpy(&dst4[2], &dst, 8);
}

/* Install an inline hook: target -> detour, returning an executable trampoline
 * that runs target's original prologue then continues at target+16, or NULL. */
static void* install_inline_hook(void* target, void* detour) {
    if (!target) return NULL;
    uint32_t* t = (uint32_t*)target;
    if (!hook_prologue_safe(t)) { LOG("hook: unrelocatable prologue at %p", target); return NULL; }

    long ps = sysconf(_SC_PAGESIZE);
    void* tr = mmap(NULL, ps, PROT_READ | PROT_WRITE, MAP_PRIVATE | MAP_ANONYMOUS, -1, 0);
    if (tr == MAP_FAILED) { LOG("hook: trampoline mmap failed"); return NULL; }
    uint32_t* w = (uint32_t*)tr;
    memcpy(w, t, 16);                              /* saved prologue        */
    emit_abs_jump(w + 4, (uintptr_t)target + 16);  /* jump back to target+16 */
    if (mprotect(tr, ps, PROT_READ | PROT_EXEC) != 0) { LOG("hook: tramp mprotect x failed"); munmap(tr, ps); return NULL; }
    __builtin___clear_cache((char*)tr, (char*)tr + 64);

    int mode = make_writable(target, 16);
    if (!mode) { LOG("hook: target not writable (W^X blocks patching)"); munmap(tr, ps); return NULL; }
    uint32_t st[4];
    emit_abs_jump(st, (uintptr_t)detour);
    memcpy(target, st, 16);
    __builtin___clear_cache((char*)target, (char*)target + 16);
    if (mode == 2) restore_exec(target, 16);       /* had to drop X to write */
    LOG("hook: installed at %p (mode=%d) tramp=%p", target, mode, tr);
    return tr;
}

/* find a per-frame main-thread method to use as the pump; verifies its code
 * pointer lives in libil2cpp and has a hookable prologue. Prefers LateUpdate. */
static const char* PUMP_CLASSES[] = {
    "GameCamera", "GameManager", "Game", "LevelManager", "Level", "PlayerController", "Player"
};
static void* find_pump_target(const char** cls_out, const char** m_out) {
    const char* methods[] = { "LateUpdate", "Update" };
    for (unsigned c = 0; c < sizeof(PUMP_CLASSES)/sizeof(*PUMP_CLASSES); c++) {
        Il2CppClass* k = find_class("", PUMP_CLASSES[c]);
        if (!k) continue;
        for (unsigned mi = 0; mi < 2; mi++) {
            const MethodInfo* m = il2cpp_class_get_method_from_name(k, methods[mi], 0);
            if (!m) continue;
            void* code = *(void**)m;                 /* MethodInfo.methodPointer @ 0x0 */
            if (!code) continue;
            if (g_il_lo && ((uintptr_t)code < g_il_lo || (uintptr_t)code >= g_il_hi)) continue;
            if (!hook_prologue_safe((uint32_t*)code)) {
                LOG("pump cand %s.%s @%p: unhookable prologue", PUMP_CLASSES[c], methods[mi], code);
                continue;
            }
            *cls_out = PUMP_CLASSES[c]; *m_out = methods[mi];
            return code;
        }
    }
    return NULL;
}

/* the pump: original body via trampoline, then our main-thread work. IL2CPP
 * instance methods take a hidden trailing MethodInfo* — forward both x0/x1. */
typedef void (*pump_fn)(void*, void*);
static pump_fn g_pump_orig;
static void main_thread_tick(void);
static void process_vel_jobs(void);   /* universal projectile-velocity scaler (below) */
static void process_anim_jobs(void);  /* animator speed-up for animation-locked throwers */
static void pump_detect_axes(void);   /* detect+equip swapped-in axes, main-thread (below) */
static void clear_projectiles_on_death(void); /* wipe fired projectiles on player death/respawn (below) */
static void respawn_redirect_tick(void);      /* move a checkpoint's respawn Y onto the 🟢 respawn marker (below) */
static void checkpoint_renumber_tick(void);   /* give every checkpoint chunk a correct sequential number (below) */
static void strip_scenery_tick(void);         /* bare-background: hide theme scenery (below) */
static void hide_hud_tick(void);              /* hide run timer / progress bar (below) */
static void camera_tick(void);                /* smooth camera / lock camera Y / contain top (below) */
static void clone_bg_tick(void);              /* wide-level scenery clone (below) */
static void flag_anim_tick(void);             /* 🚩 respawn-flag reskin/animation (below) */
static void auto_respawn_tick(void);          /* save spawn Y on any checkpoint hit; teleport there on death (below) */
static void checkpoint_respawn_tick(void);    /* reposition the def table to the chests + drain unlockCheckpoint (below) */
static void axe_motion_tick(void);    /* spin + boomerang for swapped-in axes (below)  */
static void pump_detour(void* self, void* method) {
    if (g_pump_orig) g_pump_orig(self, method);
    main_thread_tick();
}

/* ---- homing-missile adoption (main thread, via the pump) ------------------ *
 * An enemy tuned to HomingMissile now SPAWNS the missile itself (apply_tune swaps
 * its projectile prefab): on its own detection/cadence, at its own spawn point, in
 * its facing direction — exactly the trunky driving the shot. But the enemy never
 * calls the missile's setUp(), so it's born with no cannon (_hmc null): it can't
 * seek and it dies/respawns (the crash). setUp() touches Unity, so it can't run on
 * the worker thread; instead the worker finds these orphan missiles and the
 * main-thread pump ADOPTS each one — setUp(serviceCannon, dir) — after which the
 * game's own code makes it seek. dir is the launch heading (from the spawner toward
 * where the missile appeared = the enemy's facing). One hidden "service" cannon
 * (its own firing disabled) supplies the _hmc every missile needs for teardown. */
static Il2CppObject* g_cannon_prefab;     /* homingcannonUp prefab (worker-found)  */
static Il2CppObject* g_service_cannon;    /* single hidden HomingMissileCannon comp */
static Il2CppClass*  g_hmClass;           /* HomingMissile, for orphan enumeration  */
#define HM_HMC_OFF 0x20                   /* HomingMissile._hmc                     */
#define HMC_CANFIRE_OFF 0x49              /* HomingMissileCannon._canFire           */

static Il2CppClass*      g_objClass;
static Il2CppObject*     g_hmcType;        /* boxed typeof(HomingMissileCannon)     */
static const MethodInfo* g_mInstantiate;   /* Object.Instantiate(obj)               */
static const MethodInfo* g_mSetActive;     /* GameObject.SetActive(b)               */
static const MethodInfo* g_mGetComponent;  /* GameObject.GetComponent(Type)         */
static const MethodInfo* g_mSetup;         /* HomingMissile.setUp(hmc, dir)         */

/* homing-enemy positions seen this poll (worker-only; used to aim the launch) */
#define MAXHZ 64
static float g_hz[MAXHZ][2]; static int g_nhz;

/* adopt queue: worker -> pump */
typedef struct { Il2CppObject* missile; float dx, dy; } AdoptJob;
#define JOBQ 128
static AdoptJob        g_jobs[JOBQ];    static int g_njobs;
static Il2CppObject*   g_adopted[1024]; static int g_nadopted;
static pthread_mutex_t g_jlock = PTHREAD_MUTEX_INITIALIZER;

static int is_adopted_locked(Il2CppObject* m) {
    for (int i = 0; i < g_nadopted; i++) if (g_adopted[i] == m) return 1;
    return 0;
}
static void enqueue_adopt(Il2CppObject* m, float dx, float dy) {   /* worker thread */
    pthread_mutex_lock(&g_jlock);
    int dup = is_adopted_locked(m);
    if (!dup) for (int i = 0; i < g_njobs; i++) if (g_jobs[i].missile == m) { dup = 1; break; }
    if (!dup && g_njobs < JOBQ) {
        g_jobs[g_njobs].missile = m; g_jobs[g_njobs].dx = dx; g_jobs[g_njobs].dy = dy; g_njobs++;
    }
    pthread_mutex_unlock(&g_jlock);
}
/* reset between sections (a GC-reused pointer must not inherit adopted state) */
/* 🚩flag→checkpoint arming state (positions already armed; re-armed on section
 * change). Declared here so homing_reset() below can clear it. */
#define MAXARMED 64
static float g_armed[MAXARMED][2]; static int g_narmed;
static int   g_flag_step[MAXARMED];   /* per-flag reskin animation step (raise->flap) */
static void homing_reset(void) {
    pthread_mutex_lock(&g_jlock);
    g_njobs = 0; g_nadopted = 0;
    pthread_mutex_unlock(&g_jlock);
    g_narmed = 0;            /* re-arm flags after a section change / level regen */
}

/* resolve the cannon prefab by name from the loaded-object set (worker thread) */
static void resolve_cannon_prefab(void) {
    if (g_cannon_prefab || !g_findAll || !g_goType) return;
    void* args[1] = { g_goType };
    Il2CppArray* arr = find_all_locked(g_findAll, args);
    if (!arr) return;
    uintptr_t len = *(uintptr_t*)((char*)arr + 0x18);
    void** items  = (void**)((char*)arr + 0x20);
    char nm[64];
    for (uintptr_t i = 0; i < len; i++) {
        if (!items[i]) continue;
        obj_name((Il2CppObject*)items[i], nm, sizeof nm);
        if (strcmp(nm, "homingcannonUp") == 0) {
            g_cannon_prefab = (Il2CppObject*)items[i];
            LOG("homing: cannon prefab 'homingcannonUp' found @%p", (void*)g_cannon_prefab);
            return;
        }
    }
}

/* resolve the Unity/game APIs we invoke on the main thread; cached after success */
static int homing_api_ready(void) {
    if (g_mSetup) return 1;
    g_objClass       = find_class("UnityEngine", "Object");
    Il2CppClass* goc = find_class("UnityEngine", "GameObject");
    if (!g_hmClass) g_hmClass = find_class("", "HomingMissile");
    Il2CppClass* hmcc = find_class("", "HomingMissileCannon");
    if (!g_objClass || !goc || !g_hmClass || !hmcc) return 0;
    g_mInstantiate  = il2cpp_class_get_method_from_name(g_objClass, "Instantiate", 1);
    g_mSetActive    = il2cpp_class_get_method_from_name(goc, "SetActive", 1);
    g_mGetComponent = il2cpp_class_get_method_from_name(goc, "GetComponent", 1);
    g_mSetup        = il2cpp_class_get_method_from_name(g_hmClass, "setUp", 2);
    g_hmcType       = il2cpp_type_get_object(il2cpp_class_get_type(hmcc));
    if (g_mInstantiate && g_mSetActive && g_mGetComponent && g_mSetup && g_hmcType) return 1;
    LOG("homing: api resolve failed (inst=%p act=%p gc=%p setup=%p type=%p)",
        (void*)g_mInstantiate, (void*)g_mSetActive, (void*)g_mGetComponent,
        (void*)g_mSetup, (void*)g_hmcType);
    g_mSetup = NULL;   /* retry next frame */
    return 0;
}

/* MAIN THREAD: build the one hidden service cannon we hand to every missile. */
static void ensure_service_cannon(void) {
    if (g_service_cannon || !g_cannon_prefab) return;
    void* a1[1] = { g_cannon_prefab };
    Il2CppObject* go = il2cpp_runtime_invoke(g_mInstantiate, NULL, a1, NULL);
    if (!go) { LOG("homing: service cannon Instantiate null"); return; }
    uint8_t on = 1; void* a2[1] = { &on };
    il2cpp_runtime_invoke(g_mSetActive, go, a2, NULL);          /* OnEnable inits it */
    void* a3[1] = { g_hmcType };
    Il2CppObject* comp = il2cpp_runtime_invoke(g_mGetComponent, go, a3, NULL);
    if (!comp) { LOG("homing: service cannon has no HomingMissileCannon component"); return; }
    *(uint8_t*)((char*)comp + HMC_CANFIRE_OFF) = 0;             /* never fires on its own */
    g_service_cannon = comp;
    LOG("homing: service cannon ready comp=%p", (void*)g_service_cannon);
}

/* ---- shoot-cooldown defeat: machine-gun for EnemyAnimShooting (the "trunky") -----
 * Reverse-engineered EnemyAnimShooting.Update: it gates each shot on
 *   (Time.time - lastShot@0x90) >= 1.5f   // hardcoded 1.5s cooldown
 * then the shoot FrameAnimation (0xF8, played at shootingFPS@0xF0) must reach
 * SHOOT_FRAME@0x100 to actually fire. So to speed it up we (a) shove the last-shot
 * time far into the past every frame so the 1.5s cooldown always passes, and (b)
 * crank the shoot animation's fps so it reaches the fire frame fast. Fresh
 * FindObjectsOfTypeAll each call (no held pointers) + plain field writes only ->
 * safe. Gated by any firemult>1 tune. */
static Il2CppClass* g_easClass;          /* EnemyAnimShooting (anim-driven shot component) */
static Il2CppClass* g_ebClass;           /* EnemyBase (enumerated polymorphically -> all shooters) */
static const MethodInfo* g_mShootProj;   /* EnemyBase.shootProjectile() */
static int g_want_firerate;
static float g_firerate_mult = 1.0f;   /* max firemult seen -> fire rate */
/* self-contained bird spawner (avoids the game's crash-prone shootProjectile) */
static const MethodInfo* g_mInst3;    /* Object.Instantiate(Object, Vector3, Quaternion) */
static const MethodInfo* g_frSetActive, *g_frGetComp, *g_frSetVel;
static Il2CppObject*     g_frRbType;  /* typeof(Rigidbody2D) */
static int g_fr_api = -1;
static int firerate_api_ready(void) {
    if (g_fr_api >= 0) return g_fr_api;
    g_fr_api = 0;
    Il2CppClass* oc = find_class("UnityEngine", "Object");
    Il2CppClass* gc = find_class("UnityEngine", "GameObject");
    Il2CppClass* rc = find_class("UnityEngine", "Rigidbody2D");
    if (oc && gc && rc) {
        g_mInst3     = il2cpp_class_get_method_from_name(oc, "Instantiate", 3);
        g_frSetActive= il2cpp_class_get_method_from_name(gc, "SetActive", 1);
        g_frGetComp  = il2cpp_class_get_method_from_name(gc, "GetComponentInChildren", 2);  /* (Type, includeInactive) */
        g_frSetVel   = il2cpp_class_get_method_from_name(rc, "set_velocity", 1);
        g_frRbType   = il2cpp_type_get_object(il2cpp_class_get_type(rc));
        if (g_mInst3 && g_frSetActive && g_frGetComp && g_frSetVel && g_frRbType) g_fr_api = 1;
    }
    LOG("firerate: spawner api ready=%d", g_fr_api);
    return g_fr_api;
}

static void firerate_init(void) {
    for (int i = 0; i < g_ntunes; i++)
        if (g_tunes[i].firemult > 1.0f) {
            g_want_firerate = 1;
            if (g_tunes[i].firemult > g_firerate_mult) g_firerate_mult = g_tunes[i].firemult;
        }
    LOG("firerate: want=%d mult=%.1f", g_want_firerate, (double)g_firerate_mult);
}
static float g_fireacc;
/* --- test instrumentation (dedup'd by projectile name -> one line per distinct
 * projectile that actually fires; all 8 trunkies are EnemyWalking, so dedup by
 * class would collapse them — dedup by objectToShoot name shows each shot type) --- */
static char g_fired_names[48][64]; static int g_fired_n;
static void fire_log_once(Il2CppObject* eb, Il2CppClass* c) {
    char pn[64] = "?";
    Il2CppObject* proj = *(Il2CppObject**)((uint8_t*)eb + 0xF8);   /* objectToShoot */
    if (proj) obj_name(proj, pn, sizeof pn);
    for (int i = 0; i < g_fired_n; i++) if (strcmp(g_fired_names[i], pn) == 0) return;
    if (g_fired_n < 48) { strncpy(g_fired_names[g_fired_n], pn, 63); g_fired_names[g_fired_n][63]=0; g_fired_n++;
        LOG("firerate TEST: FIRED %s proj=%s", il2cpp_class_get_name(c), pn); }
}
/* one-shot census: which shooter CLASSES are present in the loaded level. The
 * non-EnemyBase ones (WoolyTrunky/Cupid/...) confirm what my loop does NOT touch. */
static const char* const g_census_classes[] = {
    "EnemyWalking","EnemyFlying","EnemyStatic","EnemyJumping","EnemyOnPath",
    "Asteroid","MotherBlob","Cannon","Cupid","WoolyTrunky","BigWoolyTrunky",
    "GiantCrab","Worm","totemBoomeranger","HomingMissileCannon",
    "HomingGhostCauldron","EnemyAnimSkullSoldier",
};
static int g_census_done;
static void census_shooters(void) {
    if (g_census_done || !g_findAll) return;
    int total = 0;
    for (unsigned k = 0; k < sizeof(g_census_classes)/sizeof(g_census_classes[0]); k++) {
        Il2CppClass* c = find_class("", g_census_classes[k]);
        if (!c) continue;
        Il2CppObject* ty = il2cpp_type_get_object(il2cpp_class_get_type(c));
        if (!ty) continue;
        void* a[1] = { ty };
        Il2CppArray* arr = find_all_locked(g_findAll, a);
        if (!arr) continue;
        uintptr_t n = *(uintptr_t*)((char*)arr + 0x18);
        if (n > 0) { LOG("firerate TEST census: %s x%lu", g_census_classes[k], (unsigned long)n); total += (int)n; }
    }
    if (total > 0) { g_census_done = 1; LOG("firerate TEST census: DONE (%d shooter-class instances)", total); }
}

static void defeat_shoot_cooldowns(void) {
    if (!g_want_firerate) return;
    census_shooters();
    if (!g_easClass) { g_easClass = find_class("", "EnemyAnimShooting");
                       if (g_easClass) LOG("firerate: EnemyAnimShooting resolved"); }
    if (!g_ebClass) { g_ebClass = find_class("", "EnemyBase");
                      if (g_ebClass) LOG("firerate: EnemyBase resolved"); }
    if (!g_ebClass || !g_findAll) return;
    if (!g_mShootProj) {                              /* EnemyBase.shootProjectile() */
        g_mShootProj = il2cpp_class_get_method_from_name(g_ebClass, "shootProjectile", 0);
        if (g_mShootProj) LOG("firerate: shootProjectile resolved");
    }
    if (!g_mShootProj) return;
    /* Enumerate the WHOLE EnemyBase family — the Unity type query is polymorphic, so
     * EnemyWalking/Flying/Static/Jumping/OnPath and every shooter subclass come back in
     * one pass. shootProjectile + state live on EnemyBase, so this is class-agnostic. */
    Il2CppObject* ty = il2cpp_type_get_object(il2cpp_class_get_type(g_ebClass));
    if (!ty) return;
    void* args[1] = { ty };
    Il2CppArray* arr = find_all_locked(g_findAll, args);
    if (!arr) return;
    uintptr_t len = *(uintptr_t*)((char*)arr + 0x18);
    void** items  = (void**)((char*)arr + 0x20);
    /* how many shots to fire this pass. Called ~every other pump frame (~30Hz); target
     * rate = 0.67 * firemult shots/s, capped so it can't overwhelm. Accumulate the
     * fractional part so low multipliers still fire at the right average rate. */
    float rate = 0.67f * g_firerate_mult; if (rate > 14.0f) rate = 14.0f;   /* machine-gun cap */
    g_fireacc += rate / 30.0f;
    int fire_now = 0;
    if (g_fireacc >= 1.0f) { fire_now = 1; g_fireacc -= 1.0f; }
    float fps = 12.0f * g_firerate_mult; if (fps > 30.0f) fps = 30.0f;   /* fast throw animation (visual) */
    int d_seen=0, d_tuned=0, d_range=0, d_shooter=0, d_fired=0;   /* TEST diagnostics */
    for (uintptr_t i = 0; i < len; i++) {
        Il2CppObject* eb = (Il2CppObject*)items[i];   /* an EnemyBase (or subclass) */
        if (!eb || !unity_alive(eb)) continue;
        d_seen++;
        /* ONLY a tuned enemy (firemult>1 on its cell) is affected. */
        Il2CppClass* ec = il2cpp_object_get_class(eb);
        const TuneClass* tc = NULL;
        for (int c = 0; c < NTC; c++) if (g_tc[c].klass == ec) { tc = &g_tc[c]; break; }
        if (!tc) continue;
        char base[80]; int col, row;
        if (!enemy_cell(eb, tc, base, sizeof base, &col, &row)) continue;
        const EnemyTune* t = match_tune(base, col, row);
        if (!t || t->firemult <= 1.0f) continue;
        d_tuned++;
        if (*(int32_t*)((uint8_t*)eb + 0x60) == 3) d_range++;
        if (*(Il2CppObject**)((uint8_t*)eb + 0xF8)) d_shooter++;
        /* If this enemy animates its shot via EnemyAnimShooting, disable the game's own
         * (miss-prone) anim-driven fire so only WE drive it: kill the 1.5s cooldown, park
         * SHOOT_FRAME out of range, crank the throw-anim fps (visual). The 0x90/0xF0/0xF8/
         * 0x100 offsets are on the EnemyAnimShooting component (eb.animBase@0x20), NOT eb.
         * Other shooter classes keep their native fire and just get extra shots below. */
        Il2CppObject* anim = *(Il2CppObject**)((uint8_t*)eb + 0x20);   /* EnemyBase.animBase */
        if (anim && unity_alive(anim) && il2cpp_object_get_class(anim) == g_easClass) {
            *(float*)((uint8_t*)anim + 0x90) = -1000.0f;
            *(int32_t*)((uint8_t*)anim + 0xF0) = (int32_t)fps;
            *(int32_t*)((uint8_t*)anim + 0x100) = 0x7fffffff;
            Il2CppObject* fa = *(Il2CppObject**)((uint8_t*)anim + 0xF8);
            if (fa && unity_alive(fa)) *(float*)((uint8_t*)fa + 0x18) = fps;
        }
        /* RANGE RULE: EnemyBase.state@0x60 == SHOOTING(3). updateShooting() sets state=3
         * only while the player is in range; out of range the movement update puts it back
         * to WALKING(0). So we fire exactly when the real enemy would — just faster — and
         * shootProjectile()'s context (transform/objectToShoot) is valid, so it can't fault
         * the way a cold out-of-range call did. Skip enemies with no projectile to shoot. */
        if (*(int32_t*)((uint8_t*)eb + 0x60) != 3) continue;   /* not SHOOTING -> skip */
        if (!*(Il2CppObject**)((uint8_t*)eb + 0xF8)) continue; /* objectToShoot null -> not a shooter */
        if (fire_now) {
            /* the game's own shot: correct spawn position + velocity, game-managed lifetime
             * (no OOM/overload), no muzzle guessing / ground-clip. */
            il2cpp_runtime_invoke(g_mShootProj, eb, NULL, NULL);
            d_fired++;
            fire_log_once(eb, ec);   /* one line per distinct projectile that fires */
        }
    }
    /* TEST: every ~1.5s show where the pipeline stands (only when enemies are loaded) */
    static int d_tick;
    if (d_seen > 0 && (++d_tick % 45) == 0)
        LOG("fireloop TEST: EnemyBase seen=%d tuned=%d inRange(st3)=%d shooters=%d fired=%d",
            d_seen, d_tuned, d_range, d_shooter, d_fired);
}

/* the pump: adopt a few orphan missiles per frame (usually none) */
static volatile long g_pump_calls;
#ifdef NATIVEMOD_DEBUG
static Il2CppObject* g_trace_m; static int g_trace_left;   /* prove one missile moves */
#endif
/* ---- checkpoint respawn markers ------------------------------------------ *
 * The game respawns via the static List<Level.CHECKPOINT_DEF> Level.checkpoints:
 * on death Level.Continue() teleports the player to the topmost UNLOCKED entry
 * below them, at worldY = entry.height - 8000. We KEEP the game's checkpoint
 * chests as the trigger, but let the author choose WHERE each checkpoint drops
 * you: place a 🟢 respawn marker (homingcannonDown) in the checkpoint's chunk and,
 * once that checkpoint is armed, we move its `height` onto the marker. The 🚩 flag
 * (homingcannonUp) + 🟢 respawn markers are HomingMissileCannons distinguished by
 * fireDir (up vs down); we also make both passive so they don't fire at the player.
 * Left/right cannons (fireDir.y≈0) are real homing enemies — left untouched.
 * Auto-active: with no respawn markers nothing is rewritten, so plain levels are
 * unaffected. We only move ARMED entries, so the game's unlock-range check (which
 * matches the chest's Y) still fires before we relocate the respawn. */
#define CP_STRIDE       0x18     /* sizeof(CHECKPOINT_DEF)                    */
#define CP_HEIGHT_OFF   0x00     /* float  height                             */
#define CP_UNLOCK_OFF   0x04     /* bool   unlocked                           */
#define HMC_FIREDIR_OFF 0x2C     /* Vector2 fireDir (x@0x2C, y@0x30)          */
#define RESPAWN_BIAS    8000.0f  /* CHECKPOINT_DEF.height = worldY + 8000      */
#define CP_RESPAWN_LIFT  80.0f   /* land 5 blocks above the chest (was 8; higher spawned inside a block) */
#define RESPAWN_FLAG_DROP 80.0f  /* 🚩respawn_flags: land ~5 blocks below the flag so touch+fall still counts */
static Il2CppClass*  g_hmcClass2;
static Il2CppObject* g_hmcTypeObj;    /* boxed typeof(HomingMissileCannon)   */
static Il2CppClass*  g_levelClass;
static FieldInfo*    g_fCheckpoints;
static long          g_respawn_logged;
/* standalone flag = checkpoint: arm a real Level.checkpoints entry when the
 * player climbs past a 🚩flag, so no game chest is needed. */
static Il2CppObject*     g_tmType;         /* boxed typeof(TileMap)            */
static Il2CppObject*     g_playerType;     /* boxed typeof(Player)            */
static const MethodInfo* g_mAddCheckpoint; /* Level.AddCheckpoint(float,bool,TileMap) */
#define TM_LEVEL_OFF 0x20                  /* TileMap.level                   */

static int armed_flag(float x, float y) {
    for (int i = 0; i < g_narmed; i++) {
        float dx = g_armed[i][0]-x, dy = g_armed[i][1]-y;
        if (dx*dx + dy*dy < 4.0f) return 1;
    }
    return 0;
}
static int armed_index(float x, float y) {   /* which armed slot a flag is in, or -1 */
    for (int i = 0; i < g_narmed; i++) {
        float dx = g_armed[i][0]-x, dy = g_armed[i][1]-y;
        if (dx*dx + dy*dy < 4.0f) return i;
    }
    return -1;
}
/* the live Player (cached; re-found via FindObjectsOfTypeAll when dead/null) */
static Il2CppObject* g_player;
static Il2CppObject* get_player(void) {
    if (g_player && unity_alive(g_player)) return g_player;
    g_player = NULL;
    if (!g_playerType) { Il2CppClass* pc = find_class("", "Player");
        if (pc) g_playerType = il2cpp_type_get_object(il2cpp_class_get_type(pc)); }
    if (!g_playerType) return NULL;
    void* args[1] = { g_playerType };
    Il2CppArray* arr = find_all_locked(g_findAll, args);
    if (!arr) return NULL;
    uintptr_t len = *(uintptr_t*)((char*)arr + 0x18);
    void** items  = (void**)((char*)arr + 0x20);
    for (uintptr_t i = 0; i < len; i++) {
        Il2CppObject* pl = (Il2CppObject*)items[i];
        if (pl && unity_alive(pl)) { g_player = pl; break; }
    }
    return g_player;
}
/* current player world Y; 0 = unknown */
static int player_worldY(float* out) {
    Il2CppObject* pl = get_player(); if (!pl) return 0;
    float p[3]; if (obj_position(pl, p)) { *out = p[1]; return 1; }
    return 0;
}
/* While the player is respawning, the game hard-forces X to the play lane (100)
 * and only stores the checkpoint's Y — so pin the player to the EXACT 🟢 respawn
 * cell (both X and Y) of whichever linked chunk it landed in. Runs every frame so
 * there's no visible snap. Only fires during Player.respawning, never in play. */
#define PLAYER_DYING_OFF     0x52D
#define PLAYER_RESPAWNING_OFF 0x52E
static void force_exact_respawn(void) {
    if (g_nrlinks == 0 && !g_pt_respawn) return;
    Il2CppObject* pl = get_player(); if (!pl) return;
    if (!*(unsigned char*)((char*)pl + PLAYER_RESPAWNING_OFF)) return;  /* only during respawn */
    float p[3]; if (!obj_position(pl, p)) return;
    /* LINK path: pin to the 🟢 respawn marker cell of the player's chunk. */
    if (g_nrlinks > 0) {
        const ChunkReg* cr = chunk_for_pos(p[1]);
        if (cr) for (int r = 0; r < g_nrlinks; r++) {
            if (strcmp(g_rlinks[r].chunk, cr->name) != 0) continue;
            float rx = cr->ox + (float)g_rlinks[r].col * 16.0f + 8.0f;
            float ry = cr->oy + (float)g_rlinks[r].row * 16.0f + 8.0f;
            obj_set_position(pl, rx, ry, p[2]);   /* exact 🟢 cell (X and Y) */
            return;
        }
    }
    /* RESPAWN-FLAGS path: pin to the exact 🚩 flag we respawned at, instead of the
     * game's lane-centred drop (which lands mid-chunk). The armed flag whose
     * checkpoint height (flagY - drop) is nearest the player's respawn Y is ours. */
    if (g_pt_respawn && g_narmed > 0) {
        int best = -1; float bd = 96.0f;                      /* only match a nearby flag */
        for (int i = 0; i < g_narmed; i++) {
            float d = (g_armed[i][1] - RESPAWN_FLAG_DROP) - p[1];
            if (d < 0) d = -d;
            if (d < bd) { bd = d; best = i; }
        }
        if (best >= 0) obj_set_position(pl, g_armed[best][0], g_armed[best][1], p[2]);  /* exact flag X,Y */
    }
}
/* ---- automatic checkpoint respawn (no markers needed) -------------------- *
 * Independent of TileMap.checkpointNum / the game's index machinery.  The game
 * sets Level.currentCheckpointCounter the instant the player passes a checkpoint
 * (regardless of its number).  We watch that field: whenever it changes to a new
 * counter, the player just touched a checkpoint, so we snapshot the player's own
 * world Y as the spawn point.  While the player is respawning we pin that Y so
 * the game can never drop them back at the start.  Cleared on level change. */
static Il2CppObject* g_levelInst;
static Il2CppObject* g_levelInstType;
static Il2CppObject* get_level(void) {
    if (g_levelInst && unity_alive(g_levelInst)) return g_levelInst;
    g_levelInst = NULL;
    if (!g_levelClass) g_levelClass = find_class("", "Level");
    if (!g_levelClass) return NULL;
    if (!g_levelInstType) g_levelInstType = il2cpp_type_get_object(il2cpp_class_get_type(g_levelClass));
    if (!g_levelInstType) return NULL;
    void* args[1] = { g_levelInstType };
    Il2CppArray* arr = find_all_locked(g_findAll, args);
    if (!arr) return NULL;
    uintptr_t len = *(uintptr_t*)((char*)arr + 0x18);
    void** items  = (void**)((char*)arr + 0x20);
    for (uintptr_t i = 0; i < len; i++) {
        Il2CppObject* lv = (Il2CppObject*)items[i];
        if (lv && unity_alive(lv)) { g_levelInst = lv; break; }
    }
    return g_levelInst;
}
/* ranked checkpoint counters, published by checkpoint_renumber_tick */
struct CpItem { Il2CppObject* ctr; Il2CppObject* tm; float x, y; int rank; unsigned char armed; };
static struct CpItem g_cplist[64]; static int g_ncplist;

static Il2CppObject* g_ar_level;      /* Level instance the saved spawn belongs to */
static float         g_ar_x;          /* saved spawn world-X (the chest's own X)      */
static float         g_ar_y;          /* saved spawn world-Y                         */
static int           g_ar_have;       /* a spawn has been saved this level           */
static int           g_ar_rank;       /* highest checkpoint rank passed so far        */
static long          g_ar_logged;
static unsigned char g_cphit[64];     /* per-chest "already registered" flag (reset per level) */
#define CC_DATASAVED_OFF 0xA8         /* CheckpointCounter.dataSaved — true only when TAKEN */

/* the game's TRUE "checkpoint collected" edge: CheckpointSign.playerPassCheckpoint
 * (+0x98) goes 0->1 when the player crosses the sign's checkpoint line. This is the
 * real collect moment — NOT the counter/rosette position. */
static Il2CppClass*  g_signClass;
static Il2CppObject* g_signType;
static void*         g_sign_seen[64];
static unsigned char g_sign_pc[64];
static int           g_n_sign_seen;
#define SIGN_PASS_OFF 0x98
/* the game's own "which checkpoint was last reached" static — flips the instant a
 * checkpoint sequence starts, independent of position guessing. */
static Il2CppClass*  g_ccClass;
static FieldInfo*    g_fLastVisited;
static int           g_lastVisited_prev = -0x7fffffff;
/* Y half-window that counts as "in this checkpoint's height band".  Checkpoints
 * sit ~one chunk apart, so keep it well under that to avoid arming the next one
 * early; rank-advance-only makes ordering monotonic regardless. */
#define AR_YBAND 180.0f
static void auto_respawn_tick(void) {
    if (!g_findAll) return;
    Il2CppObject* pl = get_player(); if (!pl) return;
    float p[3]; if (!obj_position(pl, p)) return;

    Il2CppObject* lvl = get_level();
    if (lvl != g_ar_level) {          /* new level: reset all per-level state */
        g_ar_level = lvl; g_ar_have = 0; g_ar_rank = 0;
        g_n_sign_seen = 0;
        g_lastVisited_prev = -0x7fffffff;
    }

    /* GAME SIGNAL: CheckpointCounter.lastCheckpointVisited flips when a checkpoint
     * sequence starts — the authoritative "chest reached" moment. */
    if (!g_ccClass) g_ccClass = find_class("", "CheckpointCounter");
    if (g_ccClass && !g_fLastVisited)
        g_fLastVisited = il2cpp_class_get_field_from_name(g_ccClass, "lastCheckpointVisited");
    if (g_fLastVisited) {
        int lv = 0; il2cpp_field_static_get_value(g_fLastVisited, &lv);
        if (lv != g_lastVisited_prev) {
            LOG("CHECKPOINT SEQUENCE start: lastCheckpointVisited %d -> %d  (player y=%.0f)",
                g_lastVisited_prev, lv, (double)p[1]);
            g_lastVisited_prev = lv;
        }
    }

#ifdef NATIVEMOD_DEBUG
    /* MEASURE: dump every candidate collect-state per chest on change, so we can see
     * which field flips at the exact moment a chest is actually collected. */
    { static unsigned int prevsig[64];
      for (int i = 0; i < g_ncplist; i++) {
        Il2CppObject* c = g_cplist[i].ctr; if (!c || !unity_alive(c)) continue;
        unsigned char b78 = *(unsigned char*)((char*)c + 0x78);   /* bronzeSharePoint */
        unsigned char b79 = *(unsigned char*)((char*)c + 0x79);   /* silverSharePoint */
        unsigned char bA8 = *(unsigned char*)((char*)c + 0xA8);   /* dataSaved        */
        int  prize = *(int*)((char*)c + 0xAC);                    /* prize            */
        unsigned char bB4 = *(unsigned char*)((char*)c + 0xB4);   /* initialised      */
        unsigned int sig = b78 | (b79<<1) | (bA8<<2) | (bB4<<3) | ((unsigned)prize<<8);
        if (sig != prevsig[i]) {
            LOG("STATE chest#%d: shareB=%d shareS=%d dataSaved=%d init=%d prize=%d  playerY=%.0f dy=%.0f",
                g_cplist[i].rank, b78, b79, bA8, bB4, prize, (double)p[1], (double)(p[1]-g_cplist[i].y));
            prevsig[i] = sig;
        }
      } }
#endif
    unsigned char resp  = *(unsigned char*)((char*)pl + PLAYER_RESPAWNING_OFF);
    unsigned char dying = *(unsigned char*)((char*)pl + PLAYER_DYING_OFF);
#ifdef NATIVEMOD_DEBUG
    { static unsigned char pd = 0, pr = 0;
      if (dying != pd || resp != pr) {
        LOG("DEATH FSM: dying=%d respawning=%d  playerY=%.0f  saved(y=%.0f have=%d)",
            dying, resp, (double)p[1], (double)g_ar_y, g_ar_have);
        pd = dying; pr = resp;
      } }
#endif
    return;  /* DISABLED: rely on the game's native AUTO-mode respawn (mode patch) */
    (void)resp; (void)dying;

    /* teleport-on-death: while respawning, force the saved chest X and Y */
    if (resp) {
        if (g_ar_have) obj_set_position(pl, g_ar_x, g_ar_y, p[2]);
        return;
    }
    if (dying) return;   /* don't record while dying */

    /* REACHED — the game's real signal: CheckpointSign.playerPassCheckpoint (+0x98)
     * flips 0->1 when the player passes that checkpoint's line (the only field that
     * actually changes on reach — dataSaved never does for override chests). Register
     * the nearest chest, respawn there. */
    if (!g_signClass) { g_signClass = find_class("", "CheckpointSign");
        if (g_signClass) g_signType = il2cpp_type_get_object(il2cpp_class_get_type(g_signClass)); }
    if (g_signClass && g_signType && g_ncplist > 0) {
        void* sa[1] = { g_signType };
        Il2CppArray* sarr = find_all_locked(g_findAll, sa);
        if (sarr) {
            uintptr_t sn = *(uintptr_t*)((char*)sarr + 0x18);
            void** sit   = (void**)((char*)sarr + 0x20);
            for (uintptr_t j = 0; j < sn; j++) {
                Il2CppObject* sg = (Il2CppObject*)sit[j];
                if (!sg || !unity_alive(sg)) continue;
                unsigned char pc = *(unsigned char*)((char*)sg + SIGN_PASS_OFF);
                int idx = -1;
                for (int k = 0; k < g_n_sign_seen; k++) if (g_sign_seen[k] == sg) { idx = k; break; }
                if (idx < 0 && g_n_sign_seen < 64) { idx = g_n_sign_seen++; g_sign_seen[idx] = sg; g_sign_pc[idx] = pc; continue; }
                if (idx < 0) continue;
                if (pc == 1 && g_sign_pc[idx] == 0) {            /* rising edge = just passed it */
                    float sp[3];
                    if (obj_position(sg, sp)) {
                        int bi = -1; float bd = 1e18f;
                        for (int i = 0; i < g_ncplist; i++) {
                            float ddx = g_cplist[i].x - sp[0], ddy = g_cplist[i].y - sp[1];
                            float d = ddx*ddx + ddy*ddy;
                            if (d < bd) { bd = d; bi = i; }
                        }
                        if (bi >= 0 && g_cplist[bi].rank > g_ar_rank) {
                            g_ar_rank = g_cplist[bi].rank;
                            g_ar_x = g_cplist[bi].x; g_ar_y = g_cplist[bi].y; g_ar_have = 1;
                            if (!g_levelClass) g_levelClass = find_class("", "Level");
                            if (g_levelClass && lvl && g_cplist[bi].tm) {
                                if (!g_mAddCheckpoint)
                                    g_mAddCheckpoint = il2cpp_class_get_method_from_name(g_levelClass, "AddCheckpoint", 3);
                                if (g_mAddCheckpoint) {
                                    float h = g_cplist[bi].y + RESPAWN_BIAS; unsigned char u = 1;
                                    void* pr[3] = { &h, &u, g_cplist[bi].tm };
                                    il2cpp_runtime_invoke(g_mAddCheckpoint, lvl, pr, NULL);
                                }
                            }
                            LOG("REACHED: chest #%d (%.0f,%.0f) via sign(%.0f,%.0f) player y=%.0f",
                                g_ar_rank, (double)g_cplist[bi].x, (double)g_cplist[bi].y,
                                (double)sp[0], (double)sp[1], (double)p[1]);
                        }
                    }
                }
                g_sign_pc[idx] = pc;
            }
        }
    }

#ifdef NATIVEMOD_DEBUG
    { static int hb = 0;
      if ((++hb % 60) == 0) {
        float best = 1e9f; int br = 0;
        for (int i = 0; i < g_ncplist; i++) {
            float d = p[1] - g_cplist[i].y; if (d < 0) d = -d;
            if (d < best) { best = d; br = g_cplist[i].rank; }
        }
        LOG("auto_respawn hb: playerY=%.0f  nearest chest rank=%d dy=%.0f  saved(rank=%d y=%.0f have=%d)",
            (double)p[1], br, (double)best, g_ar_rank, (double)g_ar_y, g_ar_have);
      } }
#endif
}

/* the TileMap component whose transform sits at chunk origin (ox,oy) — the tile
 * AddCheckpoint needs so Continue regenerates the right chunk on respawn. */
static Il2CppObject* tilemap_for_origin(float ox, float oy) {
    if (!g_tmType) { Il2CppClass* tmc = find_class("", "TileMap");
        if (tmc) g_tmType = il2cpp_type_get_object(il2cpp_class_get_type(tmc)); }
    if (!g_tmType) return NULL;
    void* args[1] = { g_tmType };
    Il2CppArray* arr = find_all_locked(g_findAll, args);
    if (!arr) return NULL;
    uintptr_t len = *(uintptr_t*)((char*)arr + 0x18);
    void** items  = (void**)((char*)arr + 0x20);
    Il2CppObject* best = NULL; float bestd = 1e18f;
    for (uintptr_t i = 0; i < len; i++) {
        Il2CppObject* tm = (Il2CppObject*)items[i];
        if (!tm || !unity_alive(tm)) continue;
        float p[3]; if (!obj_position(tm, p)) continue;
        float dx = p[0]-ox, dy = p[1]-oy, d = dx*dx + dy*dy;
        if (d < bestd) { bestd = d; best = tm; }
    }
    return (best && bestd < 64.0f) ? best : NULL;   /* must sit ~on the origin */
}

static void respawn_redirect_tick(void) {
    if (!g_findAll || !g_goType) return;                 /* FindObjectsOfTypeAll not ready */
    if (!g_hmcClass2)  g_hmcClass2  = find_class("", "HomingMissileCannon");
    if (!g_levelClass) g_levelClass = find_class("", "Level");
    if (!g_hmcClass2 || !g_levelClass) return;
    if (!g_hmcTypeObj)   g_hmcTypeObj   = il2cpp_type_get_object(il2cpp_class_get_type(g_hmcClass2));
    if (!g_fCheckpoints) g_fCheckpoints = il2cpp_class_get_field_from_name(g_levelClass, "checkpoints");
    if (!g_hmcTypeObj || !g_fCheckpoints) return;

    if (g_nrlinks == 0 && !g_pt_respawn) return;         /* no flag links AND respawn_flags off */

    /* passivate flag/respawn marker cannons so they never shoot the player (best
     * effort by fireDir; if fireDir reads 0 at rest a marker may fire — harmless);
     * AND arm a standalone checkpoint the moment the player climbs past a 🚩flag
     * that has a respawn link — so no game chest is needed. */
    float py; int havePlayer = player_worldY(&py);
    void* args[1] = { g_hmcTypeObj };
    Il2CppArray* arr = find_all_locked(g_findAll, args);
    if (arr) {
        uintptr_t len = *(uintptr_t*)((char*)arr + 0x18);
        void** items  = (void**)((char*)arr + 0x20);
        for (uintptr_t i = 0; i < len; i++) {
            Il2CppObject* c = (Il2CppObject*)items[i];
            if (!c || c == g_service_cannon || !unity_alive(c)) continue;
            /* identify by GameObject name so REAL homing cannons keep firing:
             * 🚩 flag = "homingcannonUp", 🟢 respawn marker = "homingcannonDown". */
            Il2CppObject* go = invoke0(c, "get_gameObject"); if (!go) continue;
            char gn[48]; obj_name(go, gn, sizeof gn);
            int isFlag   = (strncmp(gn, "homingcannonUp", 14) == 0);
            int isMarker = (strncmp(gn, "homingcannonDown", 16) == 0);
            if (isFlag || isMarker)
                *(unsigned char*)((char*)c + HMC_CANFIRE_OFF) = 0;  /* passive: never fire  */
            if (!isFlag || !havePlayer || g_narmed >= MAXARMED) continue;  /* only 🚩 flags arm */
            float p[3]; if (!obj_position(c, p)) continue;
            if (p[0] == 0.0f && p[1] == 0.0f) continue;            /* skip the prefab template at origin */
            if (py < p[1] - 8.0f) continue;                        /* player not up to the flag yet */
            if (armed_flag(p[0], p[1])) continue;                  /* armed once (by position)       */
            const ChunkReg* cr = chunk_for_pos(p[1]); if (!cr) continue;
            int row = -1;
            for (int r = 0; r < g_nrlinks; r++)
                if (strcmp(g_rlinks[r].chunk, cr->name) == 0) { row = g_rlinks[r].row; break; }
            /* respawn point: a 🟢-link cell if this flag has one, else (respawn_flags
             * on) the flag's own height dropped a bit, so no 🟢 marker is needed. */
            float respawnY;
            if (row >= 0)          respawnY = cr->oy + (float)row * 16.0f + 8.0f;
            else if (g_pt_respawn) respawnY = p[1] - RESPAWN_FLAG_DROP;
            else continue;                                         /* no link, respawn_flags off */
            Il2CppObject* tm = tilemap_for_origin(cr->ox, cr->oy); if (!tm) continue;
            Il2CppObject* lvl = *(Il2CppObject**)((char*)tm + TM_LEVEL_OFF); if (!lvl) continue;
            if (!g_mAddCheckpoint)
                g_mAddCheckpoint = il2cpp_class_get_method_from_name(g_levelClass, "AddCheckpoint", 3);
            if (!g_mAddCheckpoint) continue;
            float h = respawnY + RESPAWN_BIAS;
            unsigned char u = 1; void* pr[3] = { &h, &u, tm };
            il2cpp_runtime_invoke(g_mAddCheckpoint, lvl, pr, NULL);
            g_flag_step[g_narmed] = 0;                          /* start the flag's raise animation */
            g_armed[g_narmed][0] = p[0]; g_armed[g_narmed][1] = p[1]; g_narmed++;
            LOG("flag: armed checkpoint chunk=%s respawnY=%.1f (player reached flag)",
                cr->name, (double)respawnY);
        }
    }

    /* move every ARMED checkpoint's respawn onto the 🟢 cell of its chunk's
     * flag→respawn connection. worldY = chunkOriginY + row*16 + 8 (16 units/cell,
     * cell-centre +8 — same grid as enemy_cell); height = worldY + 8000. */
    Il2CppObject* list = NULL;
    il2cpp_field_static_get_value(g_fCheckpoints, &list);
    if (!list) return;
    Il2CppArray* citems = *(Il2CppArray**)((char*)list + 0x10);   /* List._items */
    int csize           = *(int*)((char*)list + 0x18);           /* List._size  */
    static int g_prev_csize;
    if (csize < g_prev_csize) g_narmed = 0;   /* list shrank = level regenerated -> re-arm flags */
    g_prev_csize = csize;
    if (!citems || csize <= 0) return;
    char* cdata = (char*)citems + 0x20;
    for (int i = 0; i < csize; i++) {
        char* e = cdata + (size_t)i * CP_STRIDE;
        if (!*(unsigned char*)(e + CP_UNLOCK_OFF)) continue;      /* only move ARMED checkpoints */
        float h = *(float*)(e + CP_HEIGHT_OFF);
        const ChunkReg* ec = chunk_for_pos(h - RESPAWN_BIAS);     /* chunk this checkpoint sits in */
        if (!ec) continue;
        for (int r = 0; r < g_nrlinks; r++) {
            if (strcmp(g_rlinks[r].chunk, ec->name) != 0) continue;
            float want = ec->oy + (float)g_rlinks[r].row * 16.0f + 8.0f + RESPAWN_BIAS;
            if (*(float*)(e + CP_HEIGHT_OFF) != want) {
                *(float*)(e + CP_HEIGHT_OFF) = want;
                if (g_respawn_logged < 20) {
                    LOG("respawn: cp[%d] chunk=%s -> 🟢 cell row=%d worldY=%.1f",
                        i, ec->name, g_rlinks[r].row, (double)(want - RESPAWN_BIAS));
                    g_respawn_logged++;
                }
            }
            break;
        }
    }
}

/* ---- 🚩 respawn-flag reskin: draw the flag sprites on each flag cannon so it
 * LOOKS like a flag (idle until touched, then raise -> flap loop). The cannon's
 * own Update runs before the pump, so setting the sprite here (post) sticks. */
static Il2CppObject* g_flag_idle, * g_flag_raise[9], * g_flag_flap[7];
static int g_flag_ready;
static Il2CppClass* g_srClass2; static Il2CppObject* g_srType;
static const MethodInfo* g_mSetSprite, * g_mGoGetComp;
static Il2CppObject* g_coll2dType; static const MethodInfo* g_mGetCompInCh, * g_mCollSetEn;
static int load_flag_sprites(void) {
    if (g_flag_ready) return 1;
    Il2CppClass* sc = find_class("UnityEngine", "Sprite");
    if (!sc || !resolve_findall()) return 0;
    Il2CppObject* sty = il2cpp_type_get_object(il2cpp_class_get_type(sc));
    void* a[1] = { sty };
    Il2CppArray* arr = find_all_locked(g_findAll, a);
    if (!arr) return 0;
    uintptr_t n = *(uintptr_t*)((char*)arr + 0x18);
    void** it = (void**)((char*)arr + 0x20);
    for (uintptr_t i = 0; i < n; i++) {
        Il2CppObject* s = (Il2CppObject*)it[i]; if (!s) continue;
        char nm[48]; obj_name(s, nm, sizeof nm);
        if (strncmp(nm, "flag_open-", 10) == 0) {
            int idx = atoi(nm + 10);
            if (idx == 2) g_flag_idle = s;
            else if (idx >= 3 && idx <= 11) g_flag_raise[idx - 3] = s;
        } else if (strncmp(nm, "flag_flap-", 10) == 0) {
            int idx = atoi(nm + 10);
            if (idx >= 1 && idx <= 7) g_flag_flap[idx - 1] = s;
        }
    }
    int nr = 0, nf = 0;
    for (int i = 0; i < 9; i++) if (g_flag_raise[i]) nr++;
    for (int i = 0; i < 7; i++) if (g_flag_flap[i]) nf++;
    if (g_flag_idle && nr == 9 && nf == 7) { g_flag_ready = 1; LOG("flag: sprites ready (idle + raise9 + flap7)"); return 1; }
    return 0;
}
static Il2CppObject* flag_frame(int step) {   /* raise (0..8) then loop the flap */
    if (step < 9) return g_flag_raise[step];
    return g_flag_flap[(step - 9) % 7];
}
static void flag_anim_tick(void) {
    if (!g_pt_respawn) return;
    if (!g_flag_ready) {   /* scanning ALL sprites is costly — only retry occasionally until found */
        static unsigned int tries;
        if ((++tries & 31) != 0) return;
        if (!load_flag_sprites()) return;
    }
    if (!g_hmcClass2) g_hmcClass2 = find_class("", "HomingMissileCannon");
    if (!g_hmcClass2) return;
    if (!g_hmcTypeObj) g_hmcTypeObj = il2cpp_type_get_object(il2cpp_class_get_type(g_hmcClass2));
    if (!g_srClass2) g_srClass2 = find_class("UnityEngine", "SpriteRenderer");
    if (!g_srClass2) return;
    if (!g_srType) g_srType = il2cpp_type_get_object(il2cpp_class_get_type(g_srClass2));
    if (!g_mSetSprite) g_mSetSprite = il2cpp_class_get_method_from_name(g_srClass2, "set_sprite", 1);
    if (!g_mGoGetComp) { Il2CppClass* goc = find_class("UnityEngine", "GameObject");
        if (goc) { g_mGoGetComp = il2cpp_class_get_method_from_name(goc, "GetComponent", 1);
                   g_mGetCompInCh = il2cpp_class_get_method_from_name(goc, "GetComponentInChildren", 2); } }
    if (!g_coll2dType) { Il2CppClass* cc = find_class("UnityEngine", "Collider2D");
        if (cc) { g_coll2dType = il2cpp_type_get_object(il2cpp_class_get_type(cc));
                  g_mCollSetEn = il2cpp_class_get_method_from_name(cc, "set_enabled", 1); } }
    if (!g_mSetSprite || !g_mGoGetComp || !g_hmcTypeObj || !resolve_findall()) return;
    static unsigned int animdiv;
    int advance = ((++animdiv & 3) == 0);      /* advance the animation ~15fps */
    void* a[1] = { g_hmcTypeObj };
    Il2CppArray* arr = find_all_locked(g_findAll, a);
    if (!arr) return;
    uintptr_t len = *(uintptr_t*)((char*)arr + 0x18);
    void** items  = (void**)((char*)arr + 0x20);
    for (uintptr_t i = 0; i < len; i++) {
        Il2CppObject* c = (Il2CppObject*)items[i];
        if (!c || c == g_service_cannon || !unity_alive(c)) continue;
        Il2CppObject* go = invoke0(c, "get_gameObject"); if (!go) continue;
        char gn[48]; obj_name(go, gn, sizeof gn);
        if (strncmp(gn, "homingcannonUp", 14) != 0) continue;  /* ONLY 🚩 flags — real homing cannons keep working */
        float p[3]; if (!obj_position(c, p)) continue;
        if (p[0] == 0.0f && p[1] == 0.0f) continue;            /* skip the prefab template at origin */
        /* kill the flag's collider so it doesn't block/hurt the player */
        if (g_coll2dType && g_mGetCompInCh && g_mCollSetEn) {
            uint8_t inc = 1; void* cca[2] = { g_coll2dType, &inc };
            Il2CppObject* coll = il2cpp_runtime_invoke(g_mGetCompInCh, go, cca, NULL);
            if (coll) { uint8_t off = 0; void* ea[1] = { &off }; il2cpp_runtime_invoke(g_mCollSetEn, coll, ea, NULL); }
        }
        void* ga[1] = { g_srType };
        Il2CppObject* sr = il2cpp_runtime_invoke(g_mGoGetComp, go, ga, NULL);
        if (!sr) {
#ifdef NATIVEMOD_DEBUG
            { static int nosr = 0; if (nosr < 4) { nosr++; LOG("flag DIAG: no SpriteRenderer on cannon GO"); } }
#endif
            continue;
        }
        Il2CppObject* sprite = g_flag_idle;                    /* idle until touched */
        int ai = armed_index(p[0], p[1]);
        if (ai >= 0) { if (advance) g_flag_step[ai]++; sprite = flag_frame(g_flag_step[ai]); }
        if (sprite) { void* sa[1] = { sprite }; il2cpp_runtime_invoke(g_mSetSprite, sr, sa, NULL); }
    }
}

/* ---- checkpoint renumbering (main thread) --------------------------------- *
 * BOTH the ribbon and the respawn-save read the SAME value: CheckpointCounter
 * derives its cell, looks up the TileMap at that cell, and reads TileMap.check-
 * pointNum (+0xCC).  (Update -> SetRosetteNumber draws it; PlayerPassCheckpoint
 * -> CheckpointNumber() saves it for respawn.)  Edited / override checkpoints
 * come through with checkpointNum = 0, so they draw "0" AND respawn at the level
 * start.  The flagged (chunkNameContainsCheckpoint) TileMap is NOT the instance
 * the counter's cell-lookup returns, which is why stamping the flagged one did
 * nothing.  So: rank every CheckpointCounter by world Y (closest to spawn = #1)
 * and stamp checkpointNum on the TileMap physically CLOSEST to each counter —
 * i.e. the one its cell-lookup resolves to.  Fixes ribbon + respawn together.
 * FindObjectsOfTypeAll + get_position are Unity calls -> main thread only. */
static void checkpoint_renumber_tick(void) {
    static Il2CppClass* ktm = NULL; static size_t o_num = (size_t)-1;
    static Il2CppObject* tyTM = NULL;
    static Il2CppClass* kcc = NULL; static Il2CppObject* tyCC = NULL;
    static size_t o_cc_num = (size_t)-1, o_cc_init = (size_t)-1;
    static int tried = 0, logged = 0;
    if (!tried) {
        tried = 1;
        ktm = find_class("", "TileMap");
        if (ktm) { o_num = field_off(ktm, "checkpointNum");
                   tyTM = il2cpp_type_get_object(il2cpp_class_get_type(ktm)); }
        kcc = find_class("", "CheckpointCounter");
        if (kcc) { tyCC = il2cpp_type_get_object(il2cpp_class_get_type(kcc));
                   o_cc_num  = field_off(kcc, "checkpointNumber");   /* holds 16 - drawnNum */
                   o_cc_init = field_off(kcc, "initialised"); }      /* draw-once gate       */
        LOG("checkpoint_renumber: TileMap=%p num@0x%zx CheckpointCounter=%p num@0x%zx init@0x%zx",
            (void*)ktm, o_num, (void*)kcc, o_cc_num, o_cc_init);
    }
    if (!ktm || !tyTM || !kcc || !tyCC || o_num == (size_t)-1) return;
    if (!resolve_findall()) return;

    /* 1. every CheckpointCounter + its world position */
    void* aCC[1] = { tyCC };
    Il2CppArray* ac = find_all_locked(g_findAll, aCC);
    if (!ac) return;
    uintptr_t lc = *(uintptr_t*)((char*)ac + 0x18);
    void** ic = (void**)((char*)ac + 0x20);
    typedef struct { Il2CppObject* o; float p[3]; } cpc_t;
    cpc_t cc[64]; int ncc = 0;
    for (uintptr_t j = 0; j < lc && ncc < 64; j++) {
        Il2CppObject* c = (Il2CppObject*)ic[j];
        if (!c || !unity_alive(c)) continue;
        float p[3]; if (!obj_position(c, p)) continue;
        cc[ncc].o = c; cc[ncc].p[0] = p[0]; cc[ncc].p[1] = p[1]; cc[ncc].p[2] = p[2]; ncc++;
    }
    if (ncc == 0) return;

    /* 2. every TileMap (snapshot the array once) */
    void* aTM[1] = { tyTM };
    Il2CppArray* at = find_all_locked(g_findAll, aTM);
    if (!at) return;
    uintptr_t lt = *(uintptr_t*)((char*)at + 0x18);
    void** it = (void**)((char*)at + 0x20);

    /* 3. for each counter, find its nearest TileMap; DROP strays — offscreen/UI
     * counters whose nearest chunk is absurdly far are not real checkpoints. */
    typedef struct { Il2CppObject* ctr; Il2CppObject* tm; float x, y; } CpMatch;
    CpMatch v[64]; int nv = 0;
    for (int a = 0; a < ncc && nv < 64; a++) {
        Il2CppObject* best = NULL; float bd = 1e18f;
        for (uintptr_t j = 0; j < lt; j++) {
            Il2CppObject* tm = (Il2CppObject*)it[j];
            if (!tm || !unity_alive(tm)) continue;
            float q[3]; if (!obj_position(tm, q)) continue;
            float dx = q[0] - cc[a].p[0], dy = q[1] - cc[a].p[1], dz = q[2] - cc[a].p[2];
            float d = dx*dx + dy*dy + dz*dz;
            if (d < bd) { bd = d; best = tm; }
        }
        if (!best || bd > 250000.0f) continue;      /* >500u from any chunk = not a real checkpoint */
        v[nv].ctr = cc[a].o; v[nv].tm = best; v[nv].x = cc[a].p[0]; v[nv].y = cc[a].p[1]; nv++;
    }
    if (nv == 0) return;

    /* sort ASCENDING by Y: rank 1 = lowest Y = bottom of the climb = reached first.
     * (In this game the player climbs toward increasing Y; spawn is the lowest Y.) */
    for (int a = 0; a < nv; a++)
        for (int b = a + 1; b < nv; b++)
            if (v[b].y < v[a].y) { CpMatch t = v[a]; v[a] = v[b]; v[b] = t; }

    /* stamp each nearest TileMap with its shown number + publish the list for auto_respawn.
     * The DRAWN number counts DOWN as you climb: bottom chest = nv (highest), top = 1.
     * (rank stays 1..nv in climb order — it's the internal ordering key.) */
    g_ncplist = nv;
    for (int a = 0; a < nv; a++) {
        int shown = nv - a;                                             /* bottom = highest */
        *(int32_t*)((char*)v[a].tm + o_num) = shown;
        g_cplist[a].ctr = v[a].ctr; g_cplist[a].tm = v[a].tm;
        g_cplist[a].x = v[a].x; g_cplist[a].y = v[a].y; g_cplist[a].rank = a + 1;
        /* the rosette draws ONCE (gated by 'initialised'); if it drew the stale value
         * (checkpointNumber holds 16 - drawnNum), clear the gate so Update redraws it
         * with the number we just stamped. self-limiting: stops once it matches. */
        if (o_cc_num != (size_t)-1 && o_cc_init != (size_t)-1) {
            int drawn_field = *(int32_t*)((char*)v[a].ctr + o_cc_num);   /* == 16 - drawnNum */
            if (drawn_field != 16 - shown)
                *(uint8_t*)((char*)v[a].ctr + o_cc_init) = 0;           /* force one redraw */
        }
    }
    if (!logged) { logged = 1;
        LOG("checkpoint_renumber: stamped %d checkpoint(s) %d..1 (bottom highest)", nv, nv);
        for (int a = 0; a < nv; a++)
            LOG("checkpoint_renumber:   chest rank%d shows #%d  x=%.1f  y=%.1f",
                a + 1, nv - a, (double)v[a].x, (double)v[a].y);
    }
#ifdef NATIVEMOD_DEBUG
    { static int dumped = 0;
      if (!dumped) {
        if (!g_levelClass) g_levelClass = find_class("", "Level");
        if (g_levelClass && !g_fCheckpoints)
            g_fCheckpoints = il2cpp_class_get_field_from_name(g_levelClass, "checkpoints");
        if (g_fCheckpoints) {
            Il2CppObject* list = NULL; il2cpp_field_static_get_value(g_fCheckpoints, &list);
            if (list) {
                Il2CppArray* ci = *(Il2CppArray**)((char*)list + 0x10);
                int cs = *(int*)((char*)list + 0x18);
                LOG("Level.checkpoints: size=%d", cs);
                if (ci) { char* cd = (char*)ci + 0x20;
                    for (int i = 0; i < cs && i < 20; i++) {
                        char* e = cd + (size_t)i * CP_STRIDE;
                        LOG("  Lcp[%d] worldY=%.1f unlocked=%d",
                            i, (double)(*(float*)(e+CP_HEIGHT_OFF) - RESPAWN_BIAS),
                            *(uint8_t*)(e+CP_UNLOCK_OFF)); } }
                dumped = 1;
            }
        }
      } }
#endif
}

/* the game calls unlockCheckpoint(h) with a value that DOESN'T match the def table in
 * an edited level, so its own unlock finds nothing. The hook (below) enqueues h here;
 * this pump tick drains it, finds the real chest nearest that Y, and unlocks it. */
#define UNLOCKQ 32
static volatile float g_unlockq[UNLOCKQ];
static volatile int   g_unlockq_head, g_unlockq_tail;

/* ---- checkpoint respawn: fix the def table + hijack unlockCheckpoint ---------- *
 * In an edited level the game's respawn table (Level.checkpoints) keys each entry's
 * height off its CHUNK ORIGIN, and the game's own unlockCheckpoint(worldY) fails to
 * match those entries, so nothing unlocks and death respawns at the start (MIN_Y_POS).
 * We (1) point every entry at its real chest by matching CHECKPOINT_DEF.tilemap(0x8)
 * to the chest's TileMap, and (2) drain the hooked unlockCheckpoint calls, unlocking
 * the def of the chest nearest the passed Y ourselves. Main thread only. */
static void checkpoint_respawn_tick(void) {
    if (g_unlockq_tail == g_unlockq_head) return;   /* nothing passed since last tick */
    if (g_ncplist == 0 || !g_findAll) return;
    if (!g_levelClass) g_levelClass = find_class("", "Level");
    if (!g_levelClass) return;
    if (!g_fCheckpoints)  g_fCheckpoints  = il2cpp_class_get_field_from_name(g_levelClass, "checkpoints");
    if (!g_mAddCheckpoint) g_mAddCheckpoint = il2cpp_class_get_method_from_name(g_levelClass, "AddCheckpoint", 3);
    if (!g_fCheckpoints || !g_mAddCheckpoint) return;
    Il2CppObject* lvl = get_level();
    if (!lvl) return;

    /* drain the game's unlockCheckpoint(h) events. h ≈ the passed chest's world-Y, but
     * the existing def table has no entry there, so the game's own unlock finds nothing.
     * We ADD an unlocked entry at the real chest (height = chestY + BIAS) — respawn then
     * lands at chestY (= height - BIAS). Deduped: unlock/skip if one already exists. */
    while (g_unlockq_tail != g_unlockq_head) {
        float h = g_unlockq[g_unlockq_tail & (UNLOCKQ-1)]; g_unlockq_tail++;
        int best = -1; float bd = 1e18f;
        for (int c = 0; c < g_ncplist; c++) {
            float d = g_cplist[c].y - h; if (d < 0) d = -d;
            if (d < bd) { bd = d; best = c; }
        }
        if (best < 0) continue;
        float want = g_cplist[best].y + CP_RESPAWN_LIFT + RESPAWN_BIAS;

        /* re-read the list each time (AddCheckpoint may reallocate _items) */
        Il2CppObject* list = NULL; il2cpp_field_static_get_value(g_fCheckpoints, &list);
        Il2CppArray* items = list ? *(Il2CppArray**)((char*)list + 0x10) : NULL;
        int size = list ? *(int*)((char*)list + 0x18) : 0;
        char* data = items ? (char*)items + 0x20 : NULL;

        int done = 0;
        for (int i = 0; data && i < size; i++) {
            char* e = data + (size_t)i * CP_STRIDE;
            float hh = *(float*)(e + CP_HEIGHT_OFF);
            if (hh > want - 2.0f && hh < want + 2.0f) {      /* already have this entry */
                *(unsigned char*)(e + CP_UNLOCK_OFF) = 1; done = 1; break;
            }
        }
        if (done) {
            LOG("checkpoint_respawn: re-unlocked chest rank=%d worldY=%.0f",
                g_cplist[best].rank, (double)g_cplist[best].y);
            continue;
        }
        unsigned char u = 1; void* pr[3] = { &want, &u, g_cplist[best].tm };
        il2cpp_runtime_invoke(g_mAddCheckpoint, lvl, pr, NULL);   /* add unlocked def at the chest */
        LOG("checkpoint_respawn: ADDED respawn at chest rank=%d worldY=%.0f (unlockCheckpoint h=%.0f)",
            g_cplist[best].rank, (double)g_cplist[best].y, (double)h);
    }
}

/* ---- ghostR direction flip -------------------------------------------------
 * The game can only spawn "ghostL" (leftToRight=0); a placed "ghostR" is emitted
 * as ghostL (so it spawns at all) and listed by cell in g_grcells. Ghost.Start()
 * LATCHES the ghost's direction from leftToRight@0x71 once at spawn (FixedUpdate
 * only reads it afterward for edge checks), so we must set it BEFORE Start runs.
 * We inline-hook Ghost.Start: the detour matches the fresh ghost to its spawn
 * cell and flips it to leftToRight=1, then calls the original. */
#define GHOST_LEFTTORIGHT_OFF 0x71
static Il2CppClass* g_ghostClass;
static int gr_iabs(int v){ return v < 0 ? -v : v; }
static int ghost_cell(Il2CppObject* g, char* base, int cap, int* col, int* row){
    float ew[3]; if(!obj_position(g, ew)) return 0;
    const ChunkReg* cr = chunk_for_pos(ew[1]);
    if(!cr) return 0;
    strncpy(base, cr->name, cap - 1); base[cap - 1] = 0;
    *col = (int)lroundf((ew[0] - cr->ox - 8.0f) / 16.0f);
    *row = (int)lroundf((ew[1] - cr->oy - 8.0f) / 16.0f);
    return 1;
}
typedef void (*ghost_start_fn)(void*, void*);
static ghost_start_fn g_ghost_start_orig;
static void ghost_start_detour(void* self, void* method){
    /* Runs on the main thread just before the game's Start reads leftToRight. */
    if(self && g_ngrcells > 0){
        build_chunk_registry();          /* fresh chunk origins for this frame's scroll */
        char base[80]; int col, row;
        if(ghost_cell((Il2CppObject*)self, base, sizeof base, &col, &row)){
            for(int c = 0; c < g_ngrcells; c++){
                GhostRCell* gc = &g_grcells[c];
                if(strcmp(gc->chunk, base) == 0 && gr_iabs(row - gc->row) <= 1 && gr_iabs(col - gc->col) <= 2){
                    *(uint8_t*)((char*)self + GHOST_LEFTTORIGHT_OFF) = 1;
                    LOG("ghostR: Start hook -> leftToRight=1 at %s (%d,%d)", base, col, row);
                    break;
                }
            }
        }
    }
    if(g_ghost_start_orig) g_ghost_start_orig(self, method);
}
static void ghost_start_hook_install(void){
    if(g_ghost_start_orig || g_ngrcells == 0) return;   /* nothing to flip -> skip */
    if(!g_ghostClass) g_ghostClass = find_class("", "Ghost");
    if(!g_ghostClass){ LOG("ghostR hook: no Ghost class"); return; }
    const MethodInfo* m = il2cpp_class_get_method_from_name(g_ghostClass, "Start", 0);
    if(!m){ LOG("ghostR hook: no Ghost.Start method"); return; }
    void* code = *(void**)m;
    if(!code){ LOG("ghostR hook: null code ptr"); return; }
    if(g_il_lo && ((uintptr_t)code < g_il_lo || (uintptr_t)code >= g_il_hi)){
        LOG("ghostR hook: code %p out of il2cpp range", code); return; }
    if(!hook_prologue_safe((uint32_t*)code)){ LOG("ghostR hook: unhookable prologue @%p", code); return; }
    g_ghost_start_orig = (ghost_start_fn)install_inline_hook(code, (void*)ghost_start_detour);
    LOG("ghostR hook: Ghost.Start @%p -> %s (%d cell(s))", code,
        g_ghost_start_orig ? "INSTALLED" : "FAILED", g_ngrcells);
}

static void main_thread_tick(void) {
    long n = ++g_pump_calls;
    if (n == 1)        LOG("pump: FIRST fire — running on the main thread");
    if (n % 300 == 0)  LOG("pump: alive (fires=%ld, adopted=%d, queued=%d)", n, g_nadopted, g_njobs);
#ifdef NATIVEMOD_DEBUG
    if (g_trace_m && g_trace_left > 0) {
        if (!unity_alive(g_trace_m)) { LOG("trace: missile gone"); g_trace_m = NULL; }
        else {
            float tp[3];
            if (obj_position(g_trace_m, tp))
                LOG("trace: pos=(%.1f,%.1f) state=%d hmc=%p", (double)tp[0], (double)tp[1],
                    *(int*)((char*)g_trace_m + 0xB8), *(void**)((char*)g_trace_m + HM_HMC_OFF));
            g_trace_left--;
        }
    }
#endif
    pump_refresh_snapshots();          /* main-thread enumeration for the worker (round-robin) */
    clear_projectiles_on_death();      /* wipe piled-up fired projectiles on death/respawn */
    if (n % 150 == 0) build_chunk_registry();   /* chunk map (main thread); static per section */
    if (n % 8 == 0) respawn_redirect_tick();    /* checkpoint hit -> respawn at the 🟢 marker in that chunk */
    if (n % 20 == 0) checkpoint_renumber_tick(); /* give every checkpoint chunk a correct sequential number */
    if (n % 30 == 0) strip_scenery_tick();       /* bare-background: hide theme scenery, keep the sky */
    if (n % 30 == 0) clone_bg_tick();            /* wide-level: clone scenery onto the side screens */
    if (n % 15 == 0) hide_hud_tick();            /* hide the run timer / progression bar */
    camera_tick();                               /* smooth camera / lock camera Y / contain top (every frame) */
    flag_anim_tick();                            /* 🚩 respawn-flag look: draw flag sprites every frame */
    if (n % 4 == 0)  checkpoint_respawn_tick();  /* point the respawn table at the chests + unlock on pass */
    force_exact_respawn();                       /* pin the player to the exact 🟢 cell while respawning (every frame) */
    auto_respawn_tick();                          /* save spawn Y on any checkpoint hit; teleport there on death */
    process_vel_jobs();   /* scale fieldless-shooter projectiles (independent of homing) */
    process_anim_jobs();  /* speed up animation-locked throwers so they machine-gun       */
    if ((g_pump_calls & 1) == 0) defeat_shoot_cooldowns();  /* trunky (EnemyAnimShooting) fire rate */
    pump_detect_axes();   /* find fresh axes (main-thread collider scan), equip + launch  */
    axe_motion_tick();    /* drive each tracked axe's boomerang arc                       */
    if (!g_cannon_prefab || !homing_api_ready()) return;
    ensure_service_cannon();
    if (!g_service_cannon) return;
    for (int guard = 0; guard < 6; guard++) {
        AdoptJob j; int have = 0;
        pthread_mutex_lock(&g_jlock);
        if (g_njobs > 0) { j = g_jobs[--g_njobs]; have = 1; }
        pthread_mutex_unlock(&g_jlock);
        if (!have) break;
        Il2CppObject* m = j.missile;
        pthread_mutex_lock(&g_jlock);
        if (g_nadopted < 1024) g_adopted[g_nadopted++] = m;   /* mark, whatever happens */
        pthread_mutex_unlock(&g_jlock);
        if (!unity_alive(m)) continue;
        if (*(void**)((char*)m + HM_HMC_OFF) != NULL) continue;   /* already has a cannon */
        /* main thread: safe to touch the transform now. Aim the launch from the
         * nearest homing-enemy toward where the missile spawned (= facing). */
        float mp[3]; if (!obj_position(m, mp)) continue;
        /* FindObjectsOfTypeAll also returns the loaded HomingMissile PREFAB asset,
         * which sits at world origin — never set that up (it's not a live shot). */
        if (mp[0]*mp[0] + mp[1]*mp[1] < 4.0f) continue;
        float bx = 0, by = 1, bestd = 1e30f; int found = 0;
        for (int k = 0; k < g_nhz; k++) {
            float ex = mp[0]-g_hz[k][0], ey = mp[1]-g_hz[k][1], d = ex*ex + ey*ey;
            if (d < bestd) { bestd = d; bx = ex; by = ey; found = 1; }
        }
        float dx, dy;
        if (found) { float mag = sqrtf(bx*bx+by*by); if (mag < 0.01f) { dx=0; dy=1; } else { dx=bx/mag; dy=by/mag; } }
        else { dx = 0; dy = 1; }
        float dir[2] = { dx, dy };
        void* a[2] = { g_service_cannon, dir };            /* setUp(hmc, Vector2 dir) */
        il2cpp_runtime_invoke(g_mSetup, m, a, NULL);
        LOG("homing: ADOPTED missile=%p dir=(%.2f,%.2f)", (void*)m, (double)dx, (double)dy);
#ifdef NATIVEMOD_DEBUG
        if (!g_trace_m) { g_trace_m = m; g_trace_left = 24; }   /* trace this one's motion */
#endif
    }
}

/* WORKER: find orphan HomingMissiles (no cannon yet) and queue their pointers for
 * adoption. This ONLY reads plain fields (m_CachedPtr @0x10, _hmc @0x20) — it must
 * NOT call any Unity method on a missile: missiles churn fast, and touching one the
 * main thread is mid-destroying derefs a freed native peer and crashes. All Unity
 * work (position, setUp) happens on the pump, where destruction can't race us. */
static void scan_orphan_missiles(void) {
    if (!g_hmClass) g_hmClass = find_class("", "HomingMissile");
    if (!g_hmClass) return;
    int n = enumerate(g_hmClass);
    for (int i = 0; i < n; i++) {
        Il2CppObject* m = g_found[i];
        if (!unity_alive(m)) continue;                            /* 0x10 — mem read */
        if (*(void**)((char*)m + HM_HMC_OFF) != NULL) continue;   /* 0x20 — mem read */
        pthread_mutex_lock(&g_jlock);
        int done = is_adopted_locked(m);
        if (!done) for (int k = 0; k < g_njobs; k++) if (g_jobs[k].missile == m) { done = 1; break; }
        if (!done && g_njobs < JOBQ) { g_jobs[g_njobs].missile = m; g_jobs[g_njobs].dx = 0; g_jobs[g_njobs].dy = 0; g_njobs++; }
        pthread_mutex_unlock(&g_jlock);
    }
}

/* ---- universal projectile-velocity scaler (fieldless shooters) ------------ *
 * Shooters with a launch-speed FIELD (trunky/Cupid/asteroid) are handled in
 * apply_tune. Everyone else — the EnemyBase families that fire via shootProjectile
 * (walkers/flyers/static/jumping/on-path) plus the own-class shooters (crab,
 * skeleton, mother-blob, worm, boomeranger) — has no such field: the game gives the
 * projectile its speed at spawn, so there's nothing to pre-set. Instead we scale
 * the speed on the projectile's own Rigidbody2D right after it launches.
 *
 * THREADING: the worker may never call a Unity method on a churning object (it
 * races the object's own destruction on the main thread -> SIGSEGV in libunity).
 * So the worker only STAGES tuned shooters as raw pointers + plain data, and only
 * ENUMERATES Rigidbody2D pointers. ALL Unity work — reading each shooter's world
 * position and projectile name to build the "zones", and reading/writing each
 * projectile's velocity — happens on the pump (main thread), where destruction
 * can't race us. The pump matches each queued body to the nearest zone that fires
 * the SAME projectile and scales its velocity ONCE: (baked ?? |v|) x mult, or a
 * hard 0 for mult==0 (frozen shot). */
#define MAXVZ 64
typedef struct { float x, y, mult; const char* cls; char proj[48]; } VelZone;
static VelZone g_vz[MAXVZ]; static int g_nvz;          /* PUMP-owned (built each frame) */

/* one tuned fieldless shooter, staged by the worker for the pump to position */
typedef struct { Il2CppObject* e; float mult; size_t o_proj; const char* cls; const char* swap; } Shooter;
static Shooter g_stage[MAXVZ];    static int g_nstage;      /* worker-only, during a poll */
static Shooter g_shooters[MAXVZ]; static int g_nshooters;   /* shared, under g_vlock       */

#define VJOBQ 128
static Il2CppObject*   g_vjobs[VJOBQ];   static int g_nvjobs;    /* worker -> pump      */
static Il2CppObject*   g_veldone[1024];  static int g_nveldone;  /* scaled / ignored    */
static pthread_mutex_t g_vlock = PTHREAD_MUTEX_INITIALIZER;
static int g_want_velscale;
static int g_want_axeboom;      /* an "axe" was tuned in -> spin + boomerang it */

/* animation-speed staging (definitions of the machine-gun-animator subsystem are
 * further down; declared here so velscale_reset can clear the job queue). */
typedef struct { Il2CppObject* e; float fm; int eb; } AnimJob;  /* eb = EnemyBase shooter */
static AnimJob g_animstage[MAXVZ]; static int g_nanimstage;   /* worker only            */
static AnimJob g_animjobs[MAXVZ];  static int g_nanimjobs;    /* worker -> pump (g_vlock)*/
static int g_want_animspeed;

static Il2CppClass*      g_rbClass;    /* UnityEngine.Rigidbody2D                       */
static const MethodInfo* g_mGetVel;    /* Rigidbody2D.get_velocity() -> Vector2         */
static const MethodInfo* g_mSetVel;    /* Rigidbody2D.set_velocity(Vector2)             */

static int class_has_bake(const char* cls) {
    for (int i = 0; i < g_nbakes; i++) if (strcmp(g_bakes[i].cls, cls) == 0) return 1;
    return 0;
}
static void velscale_init(void) {
    for (int i = 0; i < g_ntunes; i++)
        if (g_tunes[i].shootmult != 1.0f) g_want_velscale = 1;
    if (g_nbakes > 0) g_want_velscale = 1;   /* a bake can bite even at mult 1 */
    LOG("velscale: want=%d", g_want_velscale);
}
static int velscale_api_ready(void) {
    if (g_mSetVel) return 1;
    if (!g_rbClass) g_rbClass = find_class("UnityEngine", "Rigidbody2D");
    if (!g_rbClass) return 0;
    g_mGetVel = il2cpp_class_get_method_from_name(g_rbClass, "get_velocity", 0);
    const MethodInfo* sv = il2cpp_class_get_method_from_name(g_rbClass, "set_velocity", 1);
    if (g_mGetVel && sv) { g_mSetVel = sv; return 1; }
    return 0;
}
static int veldone_locked(Il2CppObject* o) {
    for (int i = 0; i < g_nveldone; i++) if (g_veldone[i] == o) return 1;
    return 0;
}
static void mark_veldone(Il2CppObject* o) {   /* pump thread */
    pthread_mutex_lock(&g_vlock);
    if (!veldone_locked(o) && g_nveldone < 1024) g_veldone[g_nveldone++] = o;
    pthread_mutex_unlock(&g_vlock);
}
static void velscale_reset(void) {            /* between sections */
    pthread_mutex_lock(&g_vlock);
    g_nvjobs = 0; g_nveldone = 0; g_nshooters = 0; g_nanimjobs = 0;
    pthread_mutex_unlock(&g_vlock);
}

/* WORKER: stage one tuned fieldless shooter (plain data only — NO Unity calls). */
static void stage_shooter(Il2CppObject* e, float mult, size_t o_proj,
                          const char* cls, const char* swap) {
    if (g_nstage >= MAXVZ) return;
    /* axeboom also needs shooter positions (to aim each axe away from its thrower),
     * so keep them even when there's no velocity work to do. */
    if (mult == 1.0f && !class_has_bake(cls) && !g_want_axeboom) return;
    g_stage[g_nstage].e = e; g_stage[g_nstage].mult = mult;
    g_stage[g_nstage].o_proj = o_proj; g_stage[g_nstage].cls = cls;
    g_stage[g_nstage].swap = swap; g_nstage++;
}
/* WORKER: publish this poll's staged shooters for the pump (atomic swap under lock). */
static void commit_shooters(void) {
    pthread_mutex_lock(&g_vlock);
    memcpy(g_shooters, g_stage, (size_t)g_nstage * sizeof(Shooter));
    g_nshooters = g_nstage;
    pthread_mutex_unlock(&g_vlock);
}

/* ---- animation-speed: machine-gun for animation-LOCKED throwers -------------
 * Some shooters (the trunky/totem) gate their next shot on a fixed-length THROW
 * animation, so clamping the cooldown can't push them past ~1 shot / animation.
 * Cannon-type shooters have no such animation and already machine-gun off the
 * cooldown clamp alone. For the locked ones we ALSO bump their Animator.speed so
 * the throw clip (and its fire event) plays fast and the next shot starts sooner.
 * Unity calls run on the PUMP; the worker only stages raw pointers. */
#define ANIM_SPEED_CAP 10.0f
static Il2CppClass*      g_animClass;
static Il2CppObject*     g_animType;          /* typeof(Animator)                       */
static const MethodInfo* g_mGetCompInChild;   /* Component.GetComponentInChildren(Type)  */
static const MethodInfo* g_mSetAnimSpeed;     /* Animator.set_speed(float)              */
static int g_animspeed_ready = -1;

static void animspeed_init(void) {
    for (int i = 0; i < g_ntunes; i++)
        if (g_tunes[i].firemult > 1.0f) g_want_animspeed = 1;
    LOG("animspeed: want=%d", g_want_animspeed);
}
static int animspeed_api_ready(void) {
    if (g_animspeed_ready >= 0) return g_animspeed_ready;
    g_animspeed_ready = 0;
    Il2CppClass* cc = find_class("UnityEngine", "Component");
    g_animClass = find_class("UnityEngine", "Animator");
    if (cc && g_animClass) {
        g_mGetCompInChild = il2cpp_class_get_method_from_name(cc, "GetComponentInChildren", 1);
        g_mSetAnimSpeed   = il2cpp_class_get_method_from_name(g_animClass, "set_speed", 1);
        g_animType        = il2cpp_type_get_object(il2cpp_class_get_type(g_animClass));
        if (g_mGetCompInChild && g_mSetAnimSpeed && g_animType) g_animspeed_ready = 1;
    }
    LOG("animspeed: api ready=%d (cc=%p anim=%p gcic=%p ss=%p)", g_animspeed_ready,
        (void*)cc, (void*)g_animClass, (void*)g_mGetCompInChild, (void*)g_mSetAnimSpeed);
    return g_animspeed_ready;
}
/* WORKER: stage one animation-locked / windup-gated shooter (raw pointer only). */
static void stage_anim(Il2CppObject* e, float fm, int eb) {
    if (g_nanimstage >= MAXVZ) return;
    g_animstage[g_nanimstage].e = e; g_animstage[g_nanimstage].fm = fm;
    g_animstage[g_nanimstage].eb = eb; g_nanimstage++;
}
static void commit_animjobs(void) {
    pthread_mutex_lock(&g_vlock);
    memcpy(g_animjobs, g_animstage, (size_t)g_nanimstage * sizeof(AnimJob));
    g_nanimjobs = g_nanimstage;
    pthread_mutex_unlock(&g_vlock);
}
/* PUMP: set each staged enemy's Animator speed = fire multiplier (capped), so its
 * throw animation plays fast and it can re-fire much sooner. Idempotent — the
 * property persists, but we re-assert it each pass in case the enemy resets it. */
static void process_anim_jobs(void) {
    if (!g_want_animspeed || g_nanimjobs == 0) return;
    if (!animspeed_api_ready()) return;
    AnimJob snap[MAXVZ]; int n;
    pthread_mutex_lock(&g_vlock);
    n = g_nanimjobs; if (n > MAXVZ) n = MAXVZ;
    memcpy(snap, g_animjobs, (size_t)n * sizeof(AnimJob));
    pthread_mutex_unlock(&g_vlock);
    for (int i = 0; i < n; i++) {
        Il2CppObject* e = snap[i].e;
        if (!unity_alive(e)) continue;                 /* churned away — never touch it */
        void* a1[1] = { g_animType };
        Il2CppObject* anim = il2cpp_runtime_invoke(g_mGetCompInChild, e, a1, NULL);
        if (!anim) continue;                           /* no Animator on this enemy      */
        float spd = snap[i].fm;
        if (spd > ANIM_SPEED_CAP) spd = ANIM_SPEED_CAP;
        if (spd < 1.0f) spd = 1.0f;
        void* a2[1] = { &spd };
        il2cpp_runtime_invoke(g_mSetAnimSpeed, anim, a2, NULL);
    }
}

/* WORKER: queue every live Rigidbody2D (raw pointer, plain read only). The pump
 * filters to actual projectiles by name and ignores the rest — enemies/player get
 * marked done once and never re-queued. Rigidbodies number in the dozens. */
static void scan_projectiles(void) {
    if (g_nshooters == 0) return;                       /* no tuned shooters live */
    if (!g_rbClass) g_rbClass = find_class("UnityEngine", "Rigidbody2D");
    if (!g_rbClass) return;
    int n = enumerate(g_rbClass);
    for (int i = 0; i < n; i++) {
        Il2CppObject* rb = g_found[i];
        if (!unity_alive(rb)) continue;                 /* 0x10 — plain mem read */
        pthread_mutex_lock(&g_vlock);
        int done = veldone_locked(rb);
        if (!done) for (int k = 0; k < g_nvjobs; k++) if (g_vjobs[k] == rb) { done = 1; break; }
        if (!done && g_nvjobs < VJOBQ) g_vjobs[g_nvjobs++] = rb;
        pthread_mutex_unlock(&g_vlock);
    }
}

/* PUMP: rebuild the velocity zones from the staged shooters. All Unity reads
 * (position, projectile name) happen here, on the main thread. A shooter whose
 * projectile can't be resolved, or that fires a homing shot (it seeks), is skipped
 * — so a zone always carries a concrete projectile name and can never match (and
 * thus slow) the shooter's OWN body. */
static void build_vel_zones(void) {
    static Shooter snap[MAXVZ]; int ns;
    pthread_mutex_lock(&g_vlock);
    ns = g_nshooters; memcpy(snap, g_shooters, (size_t)ns * sizeof(Shooter));
    pthread_mutex_unlock(&g_vlock);
    int n = 0;
    for (int i = 0; i < ns && n < MAXVZ; i++) {
        Il2CppObject* e = snap[i].e;
        if (!unity_alive(e)) continue;
        float ep[3]; if (!obj_position(e, ep)) continue;
        const char* pj = snap[i].swap;                  /* swap target if any */
        char nmbuf[48]; nmbuf[0] = 0;
        if (!pj && snap[i].o_proj != (size_t)-1) {       /* else its live projectile */
            Il2CppObject* pf = *(Il2CppObject**)((uint8_t*)e + snap[i].o_proj);
            if (unity_alive(pf)) {
                char full[96]; obj_name(pf, full, sizeof full);
                chunk_basename(full, nmbuf, sizeof nmbuf); pj = nmbuf;
            }
        }
        if (!pj || !pj[0] || is_homing(pj)) continue;    /* need a concrete, non-homing name */
        g_vz[n].x = ep[0]; g_vz[n].y = ep[1]; g_vz[n].mult = snap[i].mult;
        g_vz[n].cls = snap[i].cls;
        strncpy(g_vz[n].proj, pj, sizeof g_vz[n].proj - 1);
        g_vz[n].proj[sizeof g_vz[n].proj - 1] = 0;
        n++;
    }
    g_nvz = n;
}

/* ---- axe spin + boomerang (thrower-puppet motion) ------------------------- *
 * The axe is a pure puppet: no script AND no Rigidbody2D — normally the
 * totemBoomeranger moves its transform toward a target and back and cycles its
 * spin frames. Swapped onto another shooter it just sits at the muzzle, static
 * (shootProjectile sets velocity on the projectile's Rigidbody2D, which the axe
 * doesn't have). So we give it real projectile motion ourselves, on the pump:
 *   1. DETECT: the axe has a BoxCollider2D — the worker enumerates those and
 *      queues fresh ones for the pump to name-check (get_name must run on the
 *      main thread; a churning object's name-read races destruction otherwise).
 *   2. EQUIP + LAUNCH: AddComponent<Rigidbody2D> (gravity 0, its collider is a
 *      trigger so it won't bounce off terrain), fling it horizontally away from
 *      the shooter, and set angularVelocity so it spins.
 *   3. BOOMERANG: hold the outbound heading briefly, then steer the velocity back
 *      to the throw point; destroy it once it comes home (overshoots).
 * Visibility is a separate build-time prefab edit (enable the SpriteRenderer) —
 * see enable_puppet_projectile_sprites in core/typetree.py. */
#define MAXAXE          48
/* range/speed/spin are runtime tunables (g_axe_range/speed/spin, set from the
 * config blob with baked defaults) so they're editable without a recompile. */
#define AXE_MAX_FRAMES  200       /* safety: never track one forever */
#define AXE_SEEN_MAX    8192      /* non-axe colliders already name-checked (pump-owned) */
#define AXE_SCAN_EVERY  10        /* pump frames between collider scans (~6x/sec) */
#define AXE_SCAN_BUDGET 40        /* fresh colliders name-checked per scan (bounded) */
typedef struct {
    Il2CppObject* rb;             /* the runtime-added Rigidbody2D (velocity)      */
    Il2CppObject* go;             /* its GameObject                                */
    float ox, oy;                 /* throw origin (where it returns to)            */
    float dirx, diry;             /* outward heading (unit)                        */
    float lastdist;               /* last distance-to-origin (to detect overshoot) */
    int   frames, phase, hangleft; /* phase: 0 outbound, 1 hanging, 2 inbound      */
} AxeTrack;
static AxeTrack g_axes[MAXAXE]; static int g_naxes;    /* pump-owned */

static Il2CppClass*      g_bcClass;       /* UnityEngine.BoxCollider2D (detection)  */
static Il2CppObject*     g_rb2dType;      /* typeof(Rigidbody2D)                    */
static const MethodInfo* g_mAddComp;      /* GameObject.AddComponent(Type)          */
static const MethodInfo* g_mSetGrav;      /* Rigidbody2D.set_gravityScale(float)    */
static const MethodInfo* g_mSetAngVel;    /* Rigidbody2D.set_angularVelocity(float) */
static const MethodInfo* g_mSetBodyType;  /* Rigidbody2D.set_bodyType(int)          */
static const MethodInfo* g_mSetConstr;    /* Rigidbody2D.set_constraints(int)       */
static const MethodInfo* g_mDestroy;      /* Object.Destroy(Object)                 */

/* pump-owned "seen" set of non-axe colliders (so we name-check each only once) */
static Il2CppObject* g_axeseen[AXE_SEEN_MAX]; static int g_naxeseen;

static int axeboom_api_ready(void) {
    if (g_mAddComp && g_mSetGrav && g_mDestroy && g_mSetVel && g_rb2dType) return 1;
    if (!velscale_api_ready()) return 0;                 /* gives us get/set_velocity */
    Il2CppClass* goc = find_class("UnityEngine", "GameObject");
    Il2CppClass* oc  = find_class("UnityEngine", "Object");
    Il2CppClass* rc  = find_class("UnityEngine", "Rigidbody2D");
    if (!goc || !oc || !rc) return 0;
    g_mAddComp     = il2cpp_class_get_method_from_name(goc, "AddComponent", 1);
    g_mDestroy     = il2cpp_class_get_method_from_name(oc,  "Destroy", 1);
    g_mSetGrav     = il2cpp_class_get_method_from_name(rc,  "set_gravityScale", 1);
    g_mSetAngVel   = il2cpp_class_get_method_from_name(rc,  "set_angularVelocity", 1);
    g_mSetBodyType = il2cpp_class_get_method_from_name(rc,  "set_bodyType", 1);
    g_mSetConstr   = il2cpp_class_get_method_from_name(rc,  "set_constraints", 1);
    g_rb2dType     = il2cpp_type_get_object(il2cpp_class_get_type(rc));
    if (g_mAddComp && g_mDestroy && g_mSetGrav && g_mSetAngVel && g_rb2dType) return 1;
    LOG("axeboom: api resolve failed add=%p destroy=%p grav=%p ang=%p ty=%p",
        (void*)g_mAddComp, (void*)g_mDestroy, (void*)g_mSetGrav,
        (void*)g_mSetAngVel, (void*)g_rb2dType);
    return 0;
}
/* ---- clear fired projectiles on player death/respawn -----------------------
 * At a high fire rate the game's own projectiles (bombs/coconuts/...) accumulate
 * faster than they despawn, and the section reset on death (resetEnemies) does
 * NOT remove in-flight ones — so a pile of bullets survives the respawn. On the
 * player's dying/respawning rising edge we Destroy every spawned projectile
 * instance ("X(Clone)" whose base name is a known projectile). Matching only
 * "(Clone)" leaves the source prefabs (and the player/enemies) untouched. */
static const char* const g_proj_names[] = {
    "Fireball","Snowball","big_snowball","Coconut","bomb","Bullet","Arrow","axe",
    "HomingMissile","HomingGhost","SmallBlob","GiantCrabFishBulletAnimated","bird",
    "fly","AcidBall","MudBall","TurtleSpike","ManholeMonsterShot","AsteroidMedium",
    "AsteroidSmall","KingBullet","BlobBall","Ball",
};
static int is_projectile_name(const char* base) {
    for (unsigned i = 0; i < sizeof(g_proj_names)/sizeof(g_proj_names[0]); i++)
        if (!strcmp(base, g_proj_names[i])) return 1;
    return 0;
}
static Il2CppClass* g_playerClass;
static Il2CppObject* g_player;
static int g_was_dying;
static void clear_projectiles_on_death(void) {
    if (!resolve_findall()) return;
    if (!g_playerClass) g_playerClass = find_class("", "Player");
    if (!g_playerClass) return;
    if (!g_mDestroy) {
        Il2CppClass* oc = find_class("UnityEngine", "Object");
        if (oc) g_mDestroy = il2cpp_class_get_method_from_name(oc, "Destroy", 1);
        if (!g_mDestroy) return;
    }
    /* (re)acquire the live Player instance (cached; only re-find if lost) */
    if (!g_player || !unity_alive(g_player)) {
        g_player = NULL;
        Il2CppObject* ty = il2cpp_type_get_object(il2cpp_class_get_type(g_playerClass));
        if (!ty) return;
        void* a[1] = { ty };
        Il2CppArray* arr = find_all_locked(g_findAll, a);
        if (arr) {
            uintptr_t n = *(uintptr_t*)((char*)arr + 0x18);
            void** it = (void**)((char*)arr + 0x20);
            for (uintptr_t i = 0; i < n; i++)
                if (it[i] && unity_alive((Il2CppObject*)it[i])) { g_player = (Il2CppObject*)it[i]; break; }
        }
    }
    if (!g_player) return;
    int dying = *(uint8_t*)((uint8_t*)g_player + 0x52D)    /* Player.dying     */
             || *(uint8_t*)((uint8_t*)g_player + 0x52E);   /* Player.respawning*/
    if (dying && !g_was_dying) {                            /* rising edge only */
        Il2CppClass* goClass = find_class("UnityEngine", "GameObject");
        if (goClass) {
            Il2CppObject* goType = il2cpp_type_get_object(il2cpp_class_get_type(goClass));
            void* a[1] = { goType };
            Il2CppArray* arr = goType ? find_all_locked(g_findAll, a) : NULL;
            if (arr) {
                uintptr_t n = *(uintptr_t*)((char*)arr + 0x18);
                void** it = (void**)((char*)arr + 0x20);
                char full[64], base[64]; int killed = 0;
                for (uintptr_t i = 0; i < n; i++) {
                    Il2CppObject* go = (Il2CppObject*)it[i];
                    if (!go || !unity_alive(go)) continue;
                    obj_name(go, full, sizeof full);
                    if (!strstr(full, "(Clone)")) continue;      /* spawned instances only */
                    chunk_basename(full, base, sizeof base);
                    if (!is_projectile_name(base)) continue;
                    void* da[1] = { go };
                    il2cpp_runtime_invoke(g_mDestroy, NULL, da, NULL);
                    killed++;
                }
                if (killed) LOG("respawn cleanup: destroyed %d in-flight projectile(s)", killed);
            }
        }
    }
    g_was_dying = dying;
}

static void axeboom_init(void) {
    for (int i = 0; i < g_ntunes; i++)
        if (g_tunes[i].projectile && strcmp(g_tunes[i].projectile, "axe") == 0)
            g_want_axeboom = 1;
    LOG("axeboom: want=%d", g_want_axeboom);
}
/* No between-sections reset needed for g_axes: on a scene reload every tracked
 * axe's native peer dies, so axe_motion_tick drops it via unity_alive next frame.
 * The pump-owned "seen" set is likewise self-limiting per session. */
static int axeseen_has(Il2CppObject* c) {              /* pump-only */
    for (int i = 0; i < g_naxeseen; i++) if (g_axeseen[i] == c) return 1;
    return 0;
}

/* PUMP: turn one spawned axe into a spinning, self-flying boomerang. */
static void equip_axe(Il2CppObject* go) {
    if (g_naxes >= MAXAXE) return;
    float p[3]; if (!obj_position(go, p)) return;
    /* launch horizontally AWAY from the nearest tuned shooter (its facing) */
    float dx = 1.0f, best = 1e30f;
    for (int z = 0; z < g_nvz; z++) {
        float ex = p[0]-g_vz[z].x, ey = p[1]-g_vz[z].y, d = ex*ex + ey*ey;
        if (d < best) { best = d; dx = (ex >= 0.0f) ? 1.0f : -1.0f; }
    }
    void* ac[1] = { g_rb2dType };
    Il2CppObject* rb = il2cpp_runtime_invoke(g_mAddComp, go, ac, NULL);   /* give it a body */
    if (!rb) { LOG("axeboom: AddComponent(Rigidbody2D) failed"); return; }
    float zero = 0.0f; void* gz[1] = { &zero };
    il2cpp_runtime_invoke(g_mSetGrav, rb, gz, NULL);                      /* no gravity */
    if (g_mSetBodyType) { int bt = 0; void* bz[1] = { &bt }; il2cpp_runtime_invoke(g_mSetBodyType, rb, bz, NULL); }  /* Dynamic */
    if (g_mSetConstr)   { int cz = 0; void* cc[1] = { &cz }; il2cpp_runtime_invoke(g_mSetConstr,   rb, cc, NULL); }  /* free rotation */
    float nv[2] = { dx * g_axe_speed, 0.0f }; void* va[1] = { nv };
    il2cpp_runtime_invoke(g_mSetVel, rb, va, NULL);                       /* fling */
    float av = -dx * g_axe_spin; void* ava[1] = { &av };
    il2cpp_runtime_invoke(g_mSetAngVel, rb, ava, NULL);                   /* spin */
    AxeTrack* a = &g_axes[g_naxes++];
    a->rb = rb; a->go = go; a->ox = p[0]; a->oy = p[1];
    a->dirx = dx; a->diry = 0.0f; a->lastdist = 1e30f; a->frames = 0; a->phase = 0;
    LOG("axeboom: equipped+launched axe at (%.1f,%.1f) dir=%.0f", (double)p[0], (double)p[1], (double)dx);
}

/* PUMP: find freshly-spawned axes and equip them. Enumerating BoxCollider2D (a
 * large, churning set) MUST happen on the main thread — doing it on the worker
 * races the main thread's scene load and hangs the level. Runs a few times a
 * second, name-checking a bounded number of not-yet-seen colliders per pass.
 * Non-axe colliders are remembered so we never re-check them; axe colliders are
 * deliberately NOT remembered (a GC-reused pointer must be re-checkable) — the
 * per-axe "already tracked" guard prevents double-equipping a live one. */
static void pump_detect_axes(void) {
    if (!g_want_axeboom) return;
    if ((g_pump_calls % AXE_SCAN_EVERY) != 0) return;
    if (!axeboom_api_ready()) return;
    if (!g_bcClass) g_bcClass = find_class("UnityEngine", "BoxCollider2D");
    if (!g_findAll) { g_resClass = find_class("UnityEngine", "Resources");
        if (g_resClass) g_findAll = il2cpp_class_get_method_from_name(g_resClass, "FindObjectsOfTypeAll", 1); }
    if (!g_bcClass || !g_findAll) return;
    Il2CppObject* ty = il2cpp_type_get_object(il2cpp_class_get_type(g_bcClass));
    void* args[1] = { ty };
    Il2CppArray* arr = find_all_locked(g_findAll, args);
    if (!arr) return;
    uintptr_t len = *(uintptr_t*)((char*)arr + 0x18);
    void** items  = (void**)((char*)arr + 0x20);
    build_vel_zones();                       /* fresh shooter positions for launch dir */
    int checked = 0;
    for (uintptr_t i = 0; i < len && checked < AXE_SCAN_BUDGET; i++) {
        Il2CppObject* c = (Il2CppObject*)items[i];
        if (!c || !unity_alive(c) || axeseen_has(c)) continue;
        checked++;
        Il2CppObject* go = invoke0(c, "get_gameObject");
        int isaxe = 0; char full[96], base[64];
        if (unity_alive(go)) {
            obj_name(go, full, sizeof full);
            if (strstr(full, "(Clone)")) { chunk_basename(full, base, sizeof base);
                if (strcmp(base, "axe") == 0) isaxe = 1; }
        }
        if (isaxe) {
            int tracked = 0; for (int k = 0; k < g_naxes; k++) if (g_axes[k].go == go) { tracked = 1; break; }
            if (!tracked) equip_axe(go);      /* not remembered — allow re-detection */
        } else if (g_naxeseen < AXE_SEEN_MAX) {
            g_axeseen[g_naxeseen++] = c;       /* terrain/other — remember, skip forever */
        }
    }
}

/* PUMP: every frame — drive each tracked axe's boomerang arc (spin persists via
 * the Rigidbody2D's angularVelocity, set once at launch). */
static void axe_motion_tick(void) {
    if (g_naxes == 0) return;
    if (!axeboom_api_ready()) return;
    for (int i = 0; i < g_naxes; ) {
        AxeTrack* a = &g_axes[i];
        if (!unity_alive(a->rb) || !unity_alive(a->go)) { g_axes[i] = g_axes[--g_naxes]; continue; }
        float p[3]; if (!obj_position(a->go, p)) { g_axes[i] = g_axes[--g_naxes]; continue; }
        a->frames++;
        if (a->phase == 0) {
            /* hold the outbound heading until it has flown g_axe_range units out */
            float odx = p[0]-a->ox, ody = p[1]-a->oy;
            float outd = sqrtf(odx*odx + ody*ody);
            float nv[2] = { a->dirx * g_axe_speed, 0.0f }; void* va[1] = { nv };
            il2cpp_runtime_invoke(g_mSetVel, a->rb, va, NULL);
            if (outd >= g_axe_range || a->frames >= AXE_MAX_FRAMES) {
                a->phase = 1; a->hangleft = (int)(g_axe_hang * 60.0f);   /* pump ~60fps */
            }
        } else if (a->phase == 1) {
            /* hang at the far end: freeze in place (angularVelocity keeps it spinning) */
            float z[2] = { 0.0f, 0.0f }; void* va[1] = { z };
            il2cpp_runtime_invoke(g_mSetVel, a->rb, va, NULL);
            if (a->hangleft-- <= 0 || a->frames >= AXE_MAX_FRAMES) { a->phase = 2; a->lastdist = 1e30f; }
        } else {
            float dx = a->ox - p[0], dy = a->oy - p[1];
            float dist = sqrtf(dx*dx + dy*dy);
            /* destroy the instant it reaches its origin: a catch radius of ~one
             * frame of travel so it vanishes right at the throw point (plus the
             * overshoot / max-frame checks as a backstop). */
            float catchr = g_axe_speed * 0.05f; if (catchr < 6.0f) catchr = 6.0f;
            if (dist < catchr || dist > a->lastdist || a->frames >= AXE_MAX_FRAMES) {
                void* da[1] = { a->go };
                il2cpp_runtime_invoke(g_mDestroy, NULL, da, NULL);      /* caught at origin */
                g_axes[i] = g_axes[--g_naxes];
                continue;
            }
            a->lastdist = dist;
            float inv = (dist > 0.01f) ? (g_axe_speed / dist) : g_axe_speed;
            float nv[2] = { dx * inv, dy * inv }; void* va[1] = { nv };
            il2cpp_runtime_invoke(g_mSetVel, a->rb, va, NULL);          /* steer home */
        }
        i++;
    }
}

/* PUMP: enumerate live Rigidbody2Ds and queue any fresh ones, EVERY frame. The
 * worker also scans (every ~400ms) but that's far too slow — a bullet flies past
 * the muzzle-match window before the worker sees it, so only ~1/3 got scaled. Doing
 * it here (main thread, cheap: Rigidbody2Ds number in the dozens) catches each shot
 * the frame it spawns, while it's still AT the muzzle, so every bullet is scaled.
 * Same-thread FindObjectsOfTypeAll is safe; uses its OWN item walk (no g_found race
 * with the worker). */
static void scan_projectiles_pump(void) {
    if (g_nshooters == 0) return;
    if (!g_rbClass) g_rbClass = find_class("UnityEngine", "Rigidbody2D");
    if (!g_rbClass || !g_findAll) return;
    Il2CppObject* ty = il2cpp_type_get_object(il2cpp_class_get_type(g_rbClass));
    if (!ty) return;
    void* args[1] = { ty };
    Il2CppArray* arr = find_all_locked(g_findAll, args);
    if (!arr) return;
    uintptr_t len = *(uintptr_t*)((char*)arr + 0x18);
    void** items  = (void**)((char*)arr + 0x20);
    for (uintptr_t i = 0; i < len; i++) {
        Il2CppObject* rb = (Il2CppObject*)items[i];
        if (!rb || !unity_alive(rb)) continue;
        pthread_mutex_lock(&g_vlock);
        int done = veldone_locked(rb);
        if (!done) for (int k = 0; k < g_nvjobs; k++) if (g_vjobs[k] == rb) { done = 1; break; }
        if (!done && g_nvjobs < VJOBQ) g_vjobs[g_nvjobs++] = rb;
        pthread_mutex_unlock(&g_vlock);
    }
}

/* PUMP: rebuild zones, then drain bodies and scale each matching projectile once. */
static void process_vel_jobs(void) {
    if (!velscale_api_ready()) return;
    if ((g_pump_calls & 1) == 0) scan_projectiles_pump();  /* every other frame (~33ms): catch shots at the muzzle */
    if (g_nvjobs == 0) return;               /* nothing to scale */
    build_vel_zones();                       /* fresh shooter positions for matching */
    for (int guard = 0; guard < VJOBQ; guard++) {
        Il2CppObject* rb; int have = 0;
        pthread_mutex_lock(&g_vlock);
        if (g_nvjobs > 0) { rb = g_vjobs[--g_nvjobs]; have = 1; }
        pthread_mutex_unlock(&g_vlock);
        if (!have) break;
        if (!unity_alive(rb)) continue;                 /* destroyed since enqueue */
        /* main thread: Unity calls on a churning object are safe here (destruction
         * also runs on this thread, so it can't race us between check and use). */
        Il2CppObject* go = invoke0(rb, "get_gameObject");
        if (!unity_alive(go)) { mark_veldone(rb); continue; }
        char full[96], base[64]; obj_name(go, full, sizeof full);
        if (!strstr(full, "(Clone)")) { mark_veldone(rb); continue; }   /* not a live shot */
        chunk_basename(full, base, sizeof base);        /* strips " (Clone)" */
        if (g_want_axeboom && strcmp(base, "axe") == 0) { mark_veldone(rb); continue; }
        /* the axe subsystem (spin+boomerang) owns axes — don't velocity-scale them */
        float p[3]; if (!obj_position(go, p)) { mark_veldone(rb); continue; }
        int bz = -1; float bd = 48.0f * 48.0f;          /* within ~3 tiles of a shooter */
        for (int z = 0; z < g_nvz; z++) {
            if (strcmp(g_vz[z].proj, base) != 0) continue;   /* same projectile only */
            float dx = p[0]-g_vz[z].x, dy = p[1]-g_vz[z].y, d = dx*dx + dy*dy;
            if (d < bd) { bd = d; bz = z; }
        }
        if (bz < 0) { mark_veldone(rb); continue; }     /* not a tuned shooter's shot */
        float mult  = g_vz[bz].mult;
        float baked = baked_speed(g_vz[bz].cls, base);  /* dev absolute for this combo */
        Il2CppObject* bv = il2cpp_runtime_invoke(g_mGetVel, rb, NULL, NULL);
        float vx = 0, vy = 0;
        if (bv) { float* v = (float*)((char*)bv + 0x10); vx = v[0]; vy = v[1]; }
        float mag = sqrtf(vx*vx + vy*vy);
        if (mult == 0.0f) {                             /* 0x = frozen: no launch speed */
            float nv[2] = { 0.0f, 0.0f }; void* a[1] = { nv };
            il2cpp_runtime_invoke(g_mSetVel, rb, a, NULL);
            LOG("velscale: %s |v|=%.1f -> 0 (OFF)", base, (double)mag);
            mark_veldone(rb); continue;
        }
        if (mag < 0.01f) continue;                      /* not launched yet — retry, keep */
        float target = ((baked >= 0.0f) ? baked : mag) * mult;
        float s = target / mag;
        float nv[2] = { vx*s, vy*s }; void* a[1] = { nv };
        il2cpp_runtime_invoke(g_mSetVel, rb, a, NULL);
        LOG("velscale: %s |v| %.1f -> %.1f (baked=%.1f mult=%.2f)",
            base, (double)mag, (double)target, (double)baked, (double)mult);
        mark_veldone(rb);
    }
}

static void pump_install(void) {
    find_il2cpp_range();
    LOG("pump: libil2cpp exec range %p-%p", (void*)g_il_lo, (void*)g_il_hi);
    const char* pc = NULL; const char* pm = NULL;
    void* tgt = find_pump_target(&pc, &pm);
    if (!tgt) { LOG("pump: no hookable per-frame method found"); return; }
    LOG("pump: target = %s.%s @ %p", pc, pm, tgt);
    g_pump_orig = (pump_fn)install_inline_hook(tgt, (void*)pump_detour);
    LOG("pump: %s", g_pump_orig ? "HOOK INSTALLED" : "HOOK FAILED");
}

/* ---- hijack Level.unlockCheckpoint: the game's checkpoint-REGISTRATION point --- *
 * void Level.unlockCheckpoint(float height) flips the matching CHECKPOINT_DEF's
 * unlocked=true — i.e. "this checkpoint is now a live respawn target". It's the exact
 * moment the game registers a checkpoint. Instance method: (this=x0, height=s0, mi=x1). */
typedef void (*unlockcp_fn)(void*, float, void*);
static unlockcp_fn g_unlockcp_orig;
static void unlockcp_detour(void* self, float h, void* method) {
    if (g_unlockcp_orig) g_unlockcp_orig(self, h, method);   /* let the game try its own unlock */
    int hd = g_unlockq_head; g_unlockq[hd & (UNLOCKQ-1)] = h; g_unlockq_head = hd + 1;
    LOG("unlockCheckpoint HOOK: h=%.1f (enqueued)", (double)h);
}
static void checkpoint_hook_install(void) {
    if (!g_levelClass) g_levelClass = find_class("", "Level");
    if (!g_levelClass) { LOG("cp hook: no Level class"); return; }
    const MethodInfo* m = il2cpp_class_get_method_from_name(g_levelClass, "unlockCheckpoint", 1);
    if (!m) { LOG("cp hook: no unlockCheckpoint method"); return; }
    void* code = *(void**)m;
    if (!code) { LOG("cp hook: null code ptr"); return; }
    if (g_il_lo && ((uintptr_t)code < g_il_lo || (uintptr_t)code >= g_il_hi)) {
        LOG("cp hook: code %p out of il2cpp range", code); return; }
    if (!hook_prologue_safe((uint32_t*)code)) { LOG("cp hook: unhookable prologue @%p", code); return; }
    g_unlockcp_orig = (unlockcp_fn)install_inline_hook(code, (void*)unlockcp_detour);
    LOG("cp hook: unlockCheckpoint @%p -> %s", code, g_unlockcp_orig ? "INSTALLED" : "FAILED");
}


/* ---- main loop ----------------------------------------------------------- */
/* ======================================================================== *
 *  Playtest features ported native — PHASE 1: keep-music, hide HUD, bare bg *
 *  (each gated by its Settings toggle via the p| config flags.)             *
 * ======================================================================== */

/* pure no-op by direct entry patch: overwrite the method's first instruction
 * with RET (or `movz w0,#1; ret` for a bool method). No trampoline is built, so
 * it works on ANY prologue — we never run the original. (The inline hook can't
 * relocate adrp/branch prologues, which most of these methods have.) */
static void pt_patch_ret(const char* cls, const char* method, int argc, int ret_true) {
    Il2CppClass* k = find_class("", cls);
    if (!k) { LOG("pt: no class %s", cls); return; }
    const MethodInfo* m = il2cpp_class_get_method_from_name(k, method, argc);
    if (!m) { LOG("pt: no method %s.%s/%d", cls, method, argc); return; }
    void* code = *(void**)m;
    if (!code) { LOG("pt: null code %s.%s", cls, method); return; }
    if (g_il_lo && ((uintptr_t)code < g_il_lo || (uintptr_t)code >= g_il_hi)) {
        LOG("pt: %s.%s code %p out of il2cpp range", cls, method, code); return; }
    uint32_t ins[2]; int len;
    if (ret_true) { ins[0] = 0x52800020; ins[1] = 0xD65F03C0; len = 8; }  /* movz w0,#1 ; ret */
    else          { ins[0] = 0xD65F03C0;                     len = 4; }   /* ret */
    int mode = make_writable(code, len);
    if (!mode) { LOG("pt: %s.%s not writable (W^X)", cls, method); return; }
    memcpy(code, ins, (size_t)len);
    __builtin___clear_cache((char*)code, (char*)code + len);
    if (mode == 2) restore_exec(code, len);
    LOG("pt: %s.%s -> RET patched (mode=%d)", cls, method, mode);
}

/* SetActive(false) on a GameObject. */
static void pt_setactive_false(Il2CppObject* go) {
    if (!go) return;
    const MethodInfo* m = il2cpp_class_get_method_from_name(il2cpp_object_get_class(go), "SetActive", 1);
    if (!m) return;
    unsigned char v = 0; void* args[1] = { &v };
    il2cpp_runtime_invoke(m, go, args, NULL);
}
/* unbox a boxed int returned by il2cpp_runtime_invoke (payload at +0x10). */
static int pt_unbox_int(Il2CppObject* boxed) { return boxed ? *(int*)((char*)boxed + 0x10) : 0; }

/* --- hide HUD by enumeration (no hook — InitProgressionBar has an unhookable
 * prologue). Each throttled pump tick, find every SpeedrunCanvas and deactivate
 * its timer GameObject (timerGO@0x50) and/or progression line (lineParent@0xD0)
 * + player marker (playerProgression@0xD8). Idempotent; the game re-inits the
 * bar per level, so re-hiding each tick keeps it hidden. */
static Il2CppClass* g_scClass; static Il2CppObject* g_scType;
static size_t o_sc_timerGO = (size_t)-1;
/* every progression marker on the bar: the line, the player dot, start/end
 * points, the medal/save markers, AND the two lists (checkpoint flags live in
 * checkpointsProgression). Hiding all of these clears the whole bar. */
static const char* PROG_FIELDS[] = {
    "lineParent", "playerProgression", "startProgression", "endProgression",
    "bronzeProgression", "silverProgression", "saveProgression",
    "startPointProgression", "endPointProgression", 0 };
static const char* PROG_LISTS[] = { "checkpointsProgression", "allPointsProgression", 0 };
static size_t o_prog_field[16]; static size_t o_prog_list[4]; static int g_prog_resolved;
/* hide every RectTransform in a List<RectTransform> (its GameObject). */
static void pt_hide_rect_list(Il2CppObject* list) {
    if (!list) return;
    Il2CppArray* items = *(Il2CppArray**)((char*)list + 0x10);   /* List._items */
    int sz = *(int*)((char*)list + 0x18);                        /* List._size  */
    if (!items) return;
    Il2CppObject** data = (Il2CppObject**)((char*)items + 0x20);
    for (int i = 0; i < sz; i++)
        if (data[i]) pt_setactive_false(invoke0(data[i], "get_gameObject"));
}
static void hide_hud_tick(void) {
    if (!(g_pt_hidet || g_pt_hidep)) return;
    if (!g_scClass) {
        g_scClass = find_class("", "SpeedrunCanvas");
        if (g_scClass) g_scType = il2cpp_type_get_object(il2cpp_class_get_type(g_scClass));
    }
    if (!g_scClass || !g_scType || !resolve_findall()) return;
    if (!g_prog_resolved) {
        g_prog_resolved = 1;
        o_sc_timerGO = field_off(g_scClass, "timerGO");
        for (int i = 0; PROG_FIELDS[i]; i++) o_prog_field[i] = field_off(g_scClass, PROG_FIELDS[i]);
        for (int i = 0; PROG_LISTS[i];  i++) o_prog_list[i]  = field_off(g_scClass, PROG_LISTS[i]);
        LOG("pt hud: SpeedrunCanvas resolved (timer@0x%zx)", o_sc_timerGO);
    }
    void* a[1] = { g_scType };
    Il2CppArray* arr = find_all_locked(g_findAll, a);
    if (!arr) return;
    uintptr_t ln = *(uintptr_t*)((char*)arr + 0x18);
    void** it = (void**)((char*)arr + 0x20);
    for (uintptr_t j = 0; j < ln; j++) {
        Il2CppObject* sc = (Il2CppObject*)it[j];
        if (!sc || !unity_alive(sc)) continue;
        if (g_pt_hidet && o_sc_timerGO != (size_t)-1)
            pt_setactive_false(*(Il2CppObject**)((char*)sc + o_sc_timerGO));
        if (g_pt_hidep) {
            for (int i = 0; PROG_FIELDS[i]; i++) {
                if (o_prog_field[i] == (size_t)-1) continue;
                Il2CppObject* rt = *(Il2CppObject**)((char*)sc + o_prog_field[i]);
                if (rt) pt_setactive_false(invoke0(rt, "get_gameObject"));
            }
            for (int i = 0; PROG_LISTS[i]; i++)
                if (o_prog_list[i] != (size_t)-1)
                    pt_hide_rect_list(*(Il2CppObject**)((char*)sc + o_prog_list[i]));
        }
    }
}

/* --- bare background: deactivate the theme scenery under the (static)
 * Level.backgroundsContainer, keeping the sky roots + any sky-named child.
 * Runs throttled from the pump (main thread). Idempotent. */
static int pt_name_is_sky(const char* n) {
    static const char* KW[] = {"sky","moon","star","night","space","backdrop","gradient","galaxy","aurora",0};
    char low[64]; int i = 0;
    for (; n[i] && i < 63; i++) { char c = n[i]; low[i] = (c >= 'A' && c <= 'Z') ? c + 32 : c; }
    low[i] = 0;
    for (int k = 0; KW[k]; k++) if (strstr(low, KW[k])) return 1;
    return 0;
}
static Il2CppClass* g_bgLevelClass; static FieldInfo* g_bgContainerField;
static void strip_scenery_tick(void) {
    if (!g_pt_bgbare) return;
    if (!get_level()) return;                              /* not in a level yet */
    if (!g_bgLevelClass) g_bgLevelClass = find_class("", "Level");
    if (!g_bgLevelClass) return;
    if (!g_bgContainerField)
        g_bgContainerField = il2cpp_class_get_field_from_name(g_bgLevelClass, "backgroundsContainer");
    if (!g_bgContainerField) return;
    Il2CppObject* cont = NULL; il2cpp_field_static_get_value(g_bgContainerField, &cont);
    if (!cont) return;
    Il2CppObject* ctf = invoke0(cont, "get_transform");
    if (!ctf) return;
    const MethodInfo* gc = il2cpp_class_get_method_from_name(il2cpp_object_get_class(ctf), "GetChild", 1);
    if (!gc) return;
    int n = pt_unbox_int(invoke0(ctf, "get_childCount"));
    for (int r = 0; r < n; r++) {
        int idx = r; void* a[1] = { &idx };
        Il2CppObject* rootT = il2cpp_runtime_invoke(gc, ctf, a, NULL);
        if (!rootT) continue;
        int cn = pt_unbox_int(invoke0(rootT, "get_childCount"));
        for (int i = 0; i < cn; i++) {
            int ci = i; void* ca[1] = { &ci };
            Il2CppObject* childT = il2cpp_runtime_invoke(gc, rootT, ca, NULL);
            if (!childT) continue;
            Il2CppObject* childGO = invoke0(childT, "get_gameObject");
            if (!childGO) continue;
            char nm[64]; obj_name(childGO, nm, sizeof nm);
            if (!pt_name_is_sky(nm)) pt_setactive_false(childGO);
        }
    }
}

/* ======================================================================== *
 *  PHASE 2: camera trio — smooth camera / lock camera Y / contain top       *
 *  Ported from the agent's FixedUpdate/CheckForSideRooms clamp, run in the   *
 *  pump each frame (GameCamera methods are unhookable; the pump is a fine    *
 *  last-writer). Offsets from the dump: GameCamera rightEdge@0x40 targetX@   *
 *  0x54 followSpeed@0x88; Level.chunks@0x230 (List<TileMap>); TileMap        *
 *  width@0x38 height@0x3C.                                                   *
 * ======================================================================== */
#define CAM_TILE_PX  16.0f
#define CAM_Y_EASE   0.40f   /* how fast the Y correction catches up (higher = snappier) */
#define CAM_LOOKAHEAD 48.0f  /* show 3 blocks of the NEXT chunk past the top cap */
static Il2CppClass* g_gcClass; static Il2CppObject* g_gcType;
static Il2CppClass* g_camClass;                    /* UnityEngine.Camera */
static const MethodInfo* g_getMain; static const MethodInfo* g_getOrtho;
static const MethodInfo* g_mChunkIdx;              /* Level.ChunkIndexAtY(float) */
static float g_orthoHalf;                          /* main camera half view height */
static int   g_cam_yeng; static float g_cam_smoothY;

/* chunk band [bottom,top] + width for chunk index i of Level.chunks (List<TileMap>).
 * `special` (optional) = 1 for a checkpoint / end / bonus (non-gameplay) chunk —
 * the camera should NOT box to those (they're short and make it lag). */
static int cam_chunk_at(Il2CppObject* chunks, int n, int i, float* bottom, float* top,
                        int* width, int* special) {
    if (i < 0 || i >= n) return 0;
    Il2CppArray* items = *(Il2CppArray**)((char*)chunks + 0x10);   /* List._items */
    if (!items) return 0;
    Il2CppObject* tm = ((Il2CppObject**)((char*)items + 0x20))[i];
    if (!tm || !unity_alive(tm)) return 0;
    int h = *(int*)((char*)tm + 0x3C);      /* TileMap.height */
    float p[3]; if (!obj_position(tm, p)) return 0;
    *bottom = p[1];
    *top    = p[1] + (float)h * CAM_TILE_PX;
    *width  = *(int*)((char*)tm + 0x38);    /* TileMap.width  */
    if (special) {
        int cp = *(unsigned char*)((char*)tm + 0x68);   /* chunkNameContainsCheckpoint */
        int ec = *(unsigned char*)((char*)tm + 0xD4);   /* endChunk                    */
        int br = *(int*)((char*)tm + 0xE0);             /* bonusRoomType (0 = none)    */
        *special = cp || ec || (br != 0);
    }
    return 1;
}

static void camera_tick(void) {
    if (!(g_pt_smooth || g_pt_locky)) return;
    if (!g_gcClass) {
        g_gcClass = find_class("", "GameCamera");
        if (g_gcClass) g_gcType = il2cpp_type_get_object(il2cpp_class_get_type(g_gcClass));
    }
    if (!g_gcClass || !g_gcType || !resolve_findall()) return;
    /* NEVER fight the death/respawn camera: the game centres the screen on death
     * and the death->respawn sequence waits for that centre to settle. Moving
     * the camera here stalls it (~15s timeout). Hold off while dying/respawning. */
    { Il2CppObject* pl = get_player();
      if (pl && (*(unsigned char*)((char*)pl + PLAYER_RESPAWNING_OFF) ||
                 *(unsigned char*)((char*)pl + PLAYER_DYING_OFF))) { g_cam_yeng = 0; return; } }
    if (!g_levelClass) g_levelClass = find_class("", "Level");
    if (!g_levelClass) return;
    if (!g_mChunkIdx) g_mChunkIdx = il2cpp_class_get_method_from_name(g_levelClass, "ChunkIndexAtY", 1);
    if (!g_mChunkIdx) return;

    /* the single live GameCamera */
    void* a[1] = { g_gcType };
    Il2CppArray* arr = find_all_locked(g_findAll, a);
    if (!arr) return;
    uintptr_t ln = *(uintptr_t*)((char*)arr + 0x18);
    void** it = (void**)((char*)arr + 0x20);
    Il2CppObject* cam = NULL;
    for (uintptr_t j = 0; j < ln; j++) {
        Il2CppObject* c = (Il2CppObject*)it[j];
        if (c && unity_alive(c)) { cam = c; break; }
    }
    if (!cam) return;

    float re = *(float*)((char*)cam + 0x40);       /* rightEdge (one screen ~224) */
    if (re <= 0.0f) return;
    Il2CppObject* ft = invoke0(cam, "GetFollowTransform");
    if (!ft) return;
    float fp[3]; if (!obj_position(ft, fp)) return;
    float px = fp[0], py = fp[1];

    Il2CppObject* lvl = get_level();
    if (!lvl) return;
    Il2CppObject* chunks = *(Il2CppObject**)((char*)lvl + 0x230);   /* Level.chunks */
    if (!chunks) return;
    int nchunks = *(int*)((char*)chunks + 0x18);                    /* List._size */
    if (nchunks <= 0) return;
    float yy = py; void* ia[1] = { &yy };
    int idx = pt_unbox_int(il2cpp_runtime_invoke(g_mChunkIdx, lvl, ia, NULL));

    float curBot, curTop; int curW, curSpecial = 0;
    if (!cam_chunk_at(chunks, nchunks, idx, &curBot, &curTop, &curW, &curSpecial)) { g_cam_yeng = 0; return; }
    int screens = (curW + 13) / 14; if (screens < 1) screens = 1;   /* ceil(w/14) */

    /* smooth X: set targetX; the game lerps toward it (keeps its own easing) */
    if (g_pt_smooth) {
        float half = re * 0.5f, hiX = (screens - 0.5f) * re;
        float tx = (screens <= 1) ? half : (px < half ? half : (px > hiX ? hiX : px));
        *(float*)((char*)cam + 0x54) = tx;         /* targetX */
    }

    /* Y clamp needs the main camera's ortho half-height (cached) */
    if (g_orthoHalf <= 0.0f) {
        if (!g_camClass) g_camClass = find_class("UnityEngine", "Camera");
        if (g_camClass) {
            if (!g_getMain)  g_getMain  = il2cpp_class_get_method_from_name(g_camClass, "get_main", 0);
            Il2CppObject* mc = g_getMain ? il2cpp_runtime_invoke(g_getMain, NULL, NULL, NULL) : NULL;
            if (mc) {
                if (!g_getOrtho) g_getOrtho = il2cpp_class_get_method_from_name(g_camClass, "get_orthographicSize", 0);
                Il2CppObject* ob = g_getOrtho ? il2cpp_runtime_invoke(g_getOrtho, mc, NULL, NULL) : NULL;
                if (ob) g_orthoHalf = *(float*)((char*)ob + 0x10);
            }
        }
    }
    if (g_orthoHalf <= 0.0f) { g_cam_yeng = 0; return; }

    float cpos[3]; if (!obj_position(cam, cpos)) { g_cam_yeng = 0; return; }
    float camY = cpos[1];
    /* Drive the camera from OUR own smoothed Y (not the game's follow, which
     * snaps on jumps). Keep the player OUT of the upper half: put the camera
     * centre ABOVE the player so they sit ~35% up from the bottom (well into the
     * lower half, so you see where you're climbing to), and follow UP as they
     * climb. Never scroll back down (dead zone below) — a climbing game. */
    float refY      = g_cam_yeng ? g_cam_smoothY : camY;   /* our camera Y (view centre) */
    float anchorOff = g_orthoHalf * 0.30f;                 /* camera sits this far above the player */
    float want      = py + anchorOff;                      /* centre that puts the player low */
    float dead      = (want > refY) ? want : refY;         /* follow up, else hold */
    int engage = 0; float target = 0.0f;
    if (curSpecial) {
        /* checkpoint / end / bonus chunk — DON'T box the camera to it (they're
         * short and make the view lag). Just follow the player smoothly. */
        engage = 1; target = dead;
    } else if (g_pt_locky) {
        /* frame the current chunk: always cap the bottom; CAP_TOP caps the top but
         * still shows CAM_LOOKAHEAD (3 blocks) of the next chunk above. */
        float lo = curBot + g_orthoHalf;
        float hi = g_pt_captop ? (curTop - g_orthoHalf + CAM_LOOKAHEAD) : 1e30f;
        engage = 1;
        target = (lo > hi) ? lo : (dead < lo ? lo : (dead > hi ? hi : dead));
    } else if (screens > 1) {
        /* smooth-camera: only guard the side view from a NARROWER vertical neighbour. */
        float bB, bT, aB, aT; int bW, aW;
        int hasBelow = cam_chunk_at(chunks, nchunks, idx - 1, &bB, &bT, &bW, 0);
        int hasAbove = cam_chunk_at(chunks, nchunks, idx + 1, &aB, &aT, &aW, 0);
        float lo = -1e30f, hi = 1e30f;
        if (hasBelow && bW < curW) lo = curBot + g_orthoHalf;
        if (hasAbove && aW < curW) hi = curTop - g_orthoHalf;
        int offCentre = (px < 0.0f || px > re);
        if (lo > hi) { engage = 1; target = lo; }
        else if (offCentre && (lo != -1e30f || hi != 1e30f)) {
            engage = 1; target = (dead < lo ? lo : (dead > hi ? hi : dead));
        }
    }
    if (!engage) { g_cam_yeng = 0; return; }
    if (!g_cam_yeng) g_cam_smoothY = camY;         /* seed from live Y -> smooth entry */
    float fs = *(float*)((char*)cam + 0x88);       /* followSpeed */
    float ease = (fs > 0.0f && fs <= 1.0f) ? fs : CAM_Y_EASE;
    g_cam_smoothY += (target - g_cam_smoothY) * ease;
    g_cam_yeng = 1;
    float d = g_cam_smoothY - camY; if (d < 0.0f) d = -d;
    if (d > 0.01f) obj_set_position(cam, cpos[0], g_cam_smoothY, cpos[2]);
}

/* --- wide-level background clone (keep-music+bg): copy the level's animated
 * scenery onto the side screens of a wide (28/42) level so they aren't bare.
 * Object.Instantiate each scenery root x3 at screen offsets, once per level.
 * (Narrow levels have no side screens, so the copies just sit offscreen.) */
static Il2CppObject* g_bg_cloned_lvl;   /* level whose scenery we've already cloned */
static int g_bg_settle;
static Il2CppClass* g_bgObjClass; static const MethodInfo* g_bgInstantiate, * g_bgSetParent;
static void clone_bg_tick(void) {
    if (!g_pt_keep || g_pt_bgbare) return;          /* only keep-bg, and not bare mode */
    Il2CppObject* lvl = get_level();
    if (!lvl) { g_bg_settle = 0; return; }
    if (lvl == g_bg_cloned_lvl) return;             /* already cloned this level */
    if (!g_bgLevelClass) g_bgLevelClass = find_class("", "Level");
    if (!g_bgLevelClass) return;
    if (!g_bgContainerField)
        g_bgContainerField = il2cpp_class_get_field_from_name(g_bgLevelClass, "backgroundsContainer");
    if (!g_bgContainerField) return;
    Il2CppObject* cont = NULL; il2cpp_field_static_get_value(g_bgContainerField, &cont);
    if (!cont) { g_bg_settle = 0; return; }
    Il2CppObject* ctf = invoke0(cont, "get_transform");
    if (!ctf) return;
    const MethodInfo* gc = il2cpp_class_get_method_from_name(il2cpp_object_get_class(ctf), "GetChild", 1);
    if (!gc) return;
    int n = pt_unbox_int(invoke0(ctf, "get_childCount"));
    if (n <= 0) { g_bg_settle = 0; return; }
    if (++g_bg_settle < 3) return;                  /* let the scenery settle before Instantiate */
    /* snapshot the ORIGINAL roots before adding any clones */
    Il2CppObject* roots[32]; int nr = 0;
    for (int i = 0; i < n && nr < 32; i++) {
        int ci = i; void* a[1] = { &ci };
        Il2CppObject* childT = il2cpp_runtime_invoke(gc, ctf, a, NULL);
        if (!childT) continue;
        Il2CppObject* go = invoke0(childT, "get_gameObject");
        if (go) roots[nr++] = go;
    }
    if (!g_bgObjClass) g_bgObjClass = find_class("UnityEngine", "Object");
    if (g_bgObjClass && !g_bgInstantiate) g_bgInstantiate = il2cpp_class_get_method_from_name(g_bgObjClass, "Instantiate", 1);
    if (!g_bgSetParent) g_bgSetParent = il2cpp_class_get_method_from_name(il2cpp_object_get_class(ctf), "set_parent", 1);
    if (!g_bgInstantiate || !g_bgSetParent) return;
    int offs[3] = { -224, 224, 448 };               /* one screen = 224px */
    int made = 0;
    for (int r = 0; r < nr; r++) {
        float opos[3]; if (!obj_position(roots[r], opos)) continue;
        for (int k = 0; k < 3; k++) {
            void* ia[1] = { roots[r] };
            Il2CppObject* clone = il2cpp_runtime_invoke(g_bgInstantiate, NULL, ia, NULL);
            if (!clone) continue;
            Il2CppObject* clt = invoke0(clone, "get_transform");
            if (clt) { void* pa[1] = { ctf }; il2cpp_runtime_invoke(g_bgSetParent, clt, pa, NULL); }
            obj_set_position(clone, opos[0] + (float)offs[k], opos[1], opos[2]);
            made++;
        }
    }
    g_bg_cloned_lvl = lvl;
    LOG("clone_bg: %d scenery copies on side screens", made);
}

/* install the phase-1 hooks (from the worker). Keep-music = RET-patch the
 * muffle/fade methods; hide-HUD + bare-bg run per-tick in the pump. */
static void playtest_hooks_install(void) {
    if (g_pt_keep) {
        pt_patch_ret("SoundManager", "tweenLowPass", 2, 0);
        pt_patch_ret("SoundManager", "tweenMusicVolume", 2, 0);
        pt_patch_ret("SoundManager", "stopMusic", 0, 0);
        pt_patch_ret("SoundManager", "fadeOutMusicToTarget", 1, 0);
        pt_patch_ret("SoundManager", "IsPositionOnSoundRange", 1, 1);  /* -> always true */
        /* NOTE: do NOT no-op GameCamera.ExitScreenCenter — it fires
         * onExitCenterScreenEvent, which the death->respawn flow waits on.
         * No-opping it stalls respawn ~15s (the event timeout). The muffle is
         * already killed by the SoundManager patches above. */
    }
    LOG("playtest hooks installed (keep=%d hidet=%d hidep=%d bgbare=%d)",
        g_pt_keep, g_pt_hidet, g_pt_hidep, g_pt_bgbare);
}

static void* worker(void* _) {
    /* il2cpp_domain_get() faults before il2cpp_init; wait out the init window. */
    usleep(6 * 1000 * 1000);
    for (int i = 0; i < 400; i++) { g_dom = il2cpp_domain_get(); if (g_dom) break; usleep(100*1000); }
    if (!g_dom) { LOG("il2cpp domain never came up"); return NULL; }
    il2cpp_thread_attach(g_dom);
    load_config();   /* parse the per-mod tuning table patched into this .so */

    for (int i = 0; i < NTC; i++) {
        TuneClass* tc = &g_tc[i];
        tc->klass    = find_class("", tc->cls);
        tc->o_proj   = tc->f_proj   ? field_off(tc->klass, tc->f_proj)   : (size_t)-1;
        tc->o_walk   = tc->f_walk   ? field_off(tc->klass, tc->f_walk)   : (size_t)-1;
        tc->o_health = tc->f_health ? field_off(tc->klass, tc->f_health) : (size_t)-1;
        tc->o_parent = tc->f_parent ? field_off(tc->klass, tc->f_parent) : (size_t)-1;
        tc->o_shotspeed = tc->f_shotspeed ? field_off(tc->klass, tc->f_shotspeed) : (size_t)-1;
        tc->o_firerate  = tc->f_firerate  ? field_off(tc->klass, tc->f_firerate)  : (size_t)-1;
        LOG("class %-15s klass=%p proj=0x%zX walk=0x%zX hp=0x%zX parent=0x%zX shot=0x%zX fire=0x%zX",
            tc->cls, (void*)tc->klass, tc->o_proj, tc->o_walk, tc->o_health, tc->o_parent, tc->o_shotspeed, tc->o_firerate);
        snap_slot(tc->klass);   /* pre-register so the pump snapshots it from frame 1 */
    }
    homing_init();
    velscale_init();
    axeboom_init();
    animspeed_init();
    firerate_init();
    /* the pump is the ONLY safe place for scene-touching Unity calls — needed by
     * homing adoption, the velocity scaler, the axe spin/boomerang, the animator
     * speed-up AND the checkpoint renumber/respawn work, so always install it. */
    pump_install();
    checkpoint_hook_install();   /* hijack Level.unlockCheckpoint (the registration point) */
    ghost_start_hook_install();  /* placed ghostR -> set leftToRight=1 before Ghost.Start latches direction */
    playtest_hooks_install();    /* baked playtest features: keep-music / hide-HUD / bare-bg */
    LOG("ready: %d tuning record(s) across %d class(es)", g_ntunes, NTC);
    if (g_ntunes == 0) return NULL;   /* no tuning; checkpoint work runs in the pump/hook */

    char base[80];
    for (long tick = 0; ; tick++) {
        usleep(400 * 1000);
        (void)tick;
#ifdef NATIVEMOD_DEBUG
        if (tick % 12 == 0) LOG("poll alive (tick=%ld)", tick);   /* ~every 5s */
#endif
        /* build_chunk_registry() moved to the pump (main thread) — it enumerates
         * GameObjects + reads their name/position, none of which is safe off-thread. */
        if (g_want_homing) resolve_cannon_prefab();   /* no-op once the prefab is found */
        g_nhz = 0;                   /* homing-enemy positions, refilled each poll */
        g_nstage = 0;                /* velocity-scale shooters staged this poll   */
        g_nanimstage = 0;            /* animation-locked throwers staged this poll */
        int total = 0;               /* enemies live across all classes this poll */
        for (int c = 0; c < NTC; c++) {
            TuneClass* tc = &g_tc[c];
            if (!tc->klass) continue;
            int n = enumerate(tc->klass);
            total += n;
#ifdef NATIVEMOD_DEBUG
            if (n != tc->last_n) { LOG("enum %s: %d live", tc->cls, n); tc->last_n = n; }
            if (n > 0 && !g_scan_done) { g_scan_done = 1; debug_scan_projectiles(); }
#endif
            for (int i = 0; i < n; i++) {
                Il2CppObject* e = g_found[i];
                if (!unity_alive(e)) continue;   /* destroyed wrapper — never touch it */

                int si = seen_index(e);
                const EnemyTune* t;
                if (si >= 0) {
                    /* Cache hit. A GC-reused pointer could now be a different enemy;
                     * re-check the chunk (a global "*" tune matches anything, skip). */
                    t = g_seen_tune[si];
                    if (t && !(t->chunk[0] == '*' && t->chunk[1] == 0)) {
                        int col, row;
                        if (enemy_cell(e, tc, base, sizeof base, &col, &row)
                            && strcmp(base, t->chunk) != 0) si = -1;   /* re-sight */
                    }
                }
                if (si < 0) {
                    /* First sighting: the enemy is at its spawn cell (a walker has
                     * not wandered yet). Match ONCE and cache by instance pointer. */
                    int col, row;
                    if (!enemy_cell(e, tc, base, sizeof base, &col, &row)) continue;
                    t = match_tune(base, col, row);
                    if (g_seen_n < CACHE_MAX) {
                        g_seen[g_seen_n] = e; g_seen_tune[g_seen_n] = t; g_seen_n++;
                    }
#ifdef NATIVEMOD_DEBUG
                    LOG("sighted: cls=%s chunk=%s col=%d row=%d -> %s",
                        tc->cls, base, col, row, t ? "MATCH" : "(no tune)");
#endif
                }
                if (t) {
                    apply_tune(e, t, tc);
                    /* animation-locked thrower: also speed its Animator (pump side) so
                     * the throw clip plays fast and it can re-fire — the cooldown clamp
                     * alone can't beat a fixed-length throw animation (e.g. trunky). */
                    if (g_want_animspeed && tc->o_firerate != (size_t)-1
                        && t->firemult > 1.0f)
                        stage_anim(e, t->firemult, strncmp(tc->cls, "Enemy", 5) == 0);
                    /* record homing-shooter positions so we can aim adopted missiles */
                    if (g_want_homing && is_homing(t->projectile) && g_nhz < MAXHZ) {
                        float ep[3];
                        if (obj_position(e, ep)) { g_hz[g_nhz][0]=ep[0]; g_hz[g_nhz][1]=ep[1]; g_nhz++; }
                    }
                    /* Fieldless shooter (no launch-speed field): stage it for the
                     * pump, which reads its position + projectile name and scales the
                     * shots it fires. Plain data only here — NO Unity calls on the
                     * worker (they'd race a churning enemy's destruction). */
                    if ((g_want_velscale || g_want_axeboom) && tc->o_shotspeed == (size_t)-1) {
                        float mult = (t->shootmult >= 0.0f) ? t->shootmult : 1.0f;
                        stage_shooter(e, mult, tc->o_proj, tc->cls, t->projectile);
                    }
#ifdef NATIVEMOD_DEBUG
                    LOG("applied: chunk=%s proj=%s hp=%d walk=%.1f",
                        t->chunk, t->projectile ? t->projectile : "-",
                        t->health, (double)t->walk);
#endif
                }
            }
        }
        if (g_want_velscale || g_want_axeboom) commit_shooters();   /* publish staged shooters */
        if (g_want_animspeed) commit_animjobs();                    /* publish animation-locked throwers */
        if (g_want_homing && g_cannon_prefab) scan_orphan_missiles();   /* queue adopts */
        if (g_want_velscale) scan_projectiles();    /* queue projectiles for velocity scale */
        /* axe detection runs on the PUMP (pump_detect_axes) — enumerating the large
         * BoxCollider2D set on this worker thread races scene load and hangs it. */
        /* No enemies live (between sections): forget cached instances so a GC-reused
         * pointer in the next section can't inherit a stale tuning. */
        if (total == 0 && g_seen_n) {
            g_seen_n = 0;
            g_nshotfix = 0;
            homing_reset();
            velscale_reset();
#ifdef NATIVEMOD_DEBUG
            LOG("cache cleared (no live enemies)");
#endif
        }
    }
    return NULL;
}

__attribute__((constructor))
static void nativemod_init(void) {
    LOG("libnativemod loaded (enemy tuning)");
    pthread_t th; pthread_create(&th, NULL, worker, NULL); pthread_detach(th);
}
