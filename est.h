#ifndef _EST_H_
#define _EST_H_

#include <stdint.h>
#include <stdbool.h>
#include <stddef.h>

#ifdef __cplusplus
extern "C" {
#endif

/* -------------------------------------------------------------------------- */
/* Mode Constants                                                             */
/* -------------------------------------------------------------------------- */
/* NOTE: these must stay plain unsigned literals, NOT typed casts such as
 * "((uint8_t)0u)". est.c compares them inside #if/#elif directives, and the
 * C preprocessor cannot evaluate a cast expression there -- "uint8_t" is not
 * a macro, so it is replaced with 0 per the standard, turning e.g.
 * "((uint8_t)1u)" into the malformed "((0)1u)". Plain literals still convert
 * safely and implicitly wherever these are used in ordinary C code (e.g.
 * assignment into a uint8_t struct field). */
#define EST_BF_CIRCULAR            (0u)
#define EST_BF_LINEAR              (1u)

#define EST_TS_ULEB128             (0u)
#define EST_TS_DIFF_DYN            (0u)
#define EST_TS_FULL_2B             (2u)
#define EST_TS_FULL_3B             (3u)
#define EST_TS_FULL_4B             (4u)

/* Include generated configuration macros and IDs */
#include "est_cfg.h"

#ifndef EST_TS_CNT_MASK
#define EST_TS_CNT_MASK            ((uint32_t)0xFFFFFFFFu)
#endif

/* Public engine API invoked by trace macros */
void est_log(uint32_t id, uint32_t now_ticks, const uint8_t *payload, uint16_t payload_len);

#ifdef __cplusplus
}
#endif

#endif /* _EST_H_ */