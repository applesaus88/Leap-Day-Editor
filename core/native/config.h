/* config.h — per-individual-enemy tuning table.
 *
 * This file is REGENERATED at build time by core/nativemod.py from the project's
 * `enemy_tuning` map. The version checked into the repo is just a valid empty
 * default so nativemod.c compiles standalone (and so a build with no tuning still
 * produces a well-formed .so).
 *
 * Each record keys ONE placed enemy by (chunk basename, col, rowFromBottom) — the
 * runtime match key calibrated against real spawns (see project-native-il2cpp-
 * modloader memory): col = sx, rowFromBottom = (chunkHeight-1) - sy.
 *
 *   projectile : GameObject name to set as EnemyBase.objectToShoot, or NULL to leave.
 *   health     : EnemyBase.health, or -1 to leave.
 *   walk       : EnemyWalking.velocity (walk speed), or a negative sentinel to leave.
 */
#ifndef NATIVEMOD_CONFIG_H
#define NATIVEMOD_CONFIG_H

typedef struct {
    const char* chunk;       /* chunk basename, e.g. "s17_mat_cactustornado_EASY_1" */
    int         col;         /* sx (column, 0 = left) */
    int         row;         /* rowFromBottom = (height-1) - sy */
    const char* projectile;  /* GameObject name, or NULL */
    int         health;      /* -1 = leave */
    float       walk;        /* < 0 = leave */
    float       shootmult;   /* launch-speed multiplier for this placement (1 = leave) */
    float       firemult;    /* fire-rate multiplier (1 = leave; 2 = fires twice as fast) */
} EnemyTune;

static const EnemyTune g_tunes[] = {
    /* (empty default) */
    { 0, 0, 0, 0, -1, -1.0f, 1.0f, 1.0f }
};
static const int g_ntunes = 0;   /* real builds set this to the record count */

/* Dev-baked baseline launch speed per (enemy class, projectile) combo — the
 * shipped default a placement's shootmult scales. Authored in the editor's dev
 * mode. speed < 0 means "no bake" (use the game's own value). */
typedef struct {
    const char* cls;         /* enemy class, e.g. "WoolyTrunky" */
    const char* projectile;  /* GameObject name, e.g. "HomingMissile" */
    float       speed;       /* absolute launch speed */
} ShootBake;

static const ShootBake g_bakes[] = {
    { 0, 0, -1.0f }
};
static const int g_nbakes = 0;

#endif /* NATIVEMOD_CONFIG_H */
