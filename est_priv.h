#ifndef _EST_PRIV_H_
#define _EST_PRIV_H_

#include "est.h"
#include <assert.h>

#ifdef __cplusplus
extern "C" {
#endif

/* -------------------------------------------------------------------------- */
/* Dynamic Metadata Header (Live runtime state in discoverable RAM block)      */
/* -------------------------------------------------------------------------- */
/* Naturally 4-byte aligned (8 bytes total, no compiler packing required).    */
typedef struct {
    uint32_t write_idx;   /* Current write byte offset in buffer (0 .. buffer_size - 1) */
    uint8_t  is_wrapped;  /* Circular mode: 1 if buffer wrapped at least once, 0 otherwise */
    uint8_t  is_stopped;  /* Linear mode: 1 if buffer saturated and halted, 0 otherwise */
    uint8_t  reserved[2]; /* 2-byte alignment padding (keeps struct naturally 4-byte aligned) */
} est_metadata_t;

/* -------------------------------------------------------------------------- */
/* Full RAM Block Structure                                                   */
/* -------------------------------------------------------------------------- */
typedef struct {
    uint32_t magic_start;     /* offset 0 */
    uint32_t magic_start_inv; /* offset 4 */
    est_metadata_t meta;      /* offset 8 (size 8) */
    uint8_t buffer[EST_BUFFER_SIZE]; /* offset 16 */
#if (EST_BUFFER_ALIGN_BYTES > 0u)
    uint8_t align_pad[EST_BUFFER_ALIGN_BYTES];
#endif
    uint32_t magic_end_inv;   /* guaranteed 4-byte aligned */
    uint32_t magic_end;       /* guaranteed 4-byte aligned */
} est_ram_block_t;

/* Paired sentinel values an external tool can scan target RAM for to
   locate this block without knowing its address ahead of time. Each
   magic word is immediately followed by its bitwise complement, which
   is what a scanner should verify -- checking both halves cuts false
   positive matches far below what a single 4-byte pattern would give.
   Deliberately tied to EST_SHORT_CONFIG_HASH (not an independent constant):
   a scanner match doubles as an immediate check that this is not just an
   EST buffer, but specifically this exact config/firmware version. */
#define EST_RAM_MAGIC_START     EST_SHORT_CONFIG_HASH
#define EST_RAM_MAGIC_START_INV ((uint32_t)(~EST_RAM_MAGIC_START))
#define EST_RAM_MAGIC_END       EST_SHORT_CONFIG_HASH
#define EST_RAM_MAGIC_END_INV   ((uint32_t)(~EST_RAM_MAGIC_END))

/* Compile-time check to verify memory layout and size integrity.
   Computed structurally via sizeof() so it can never drift out of sync
   with est_metadata_t's real layout in est_priv.h. */
#if defined(__STDC_VERSION__) && __STDC_VERSION__ >= 201112L
static_assert(sizeof(est_ram_block_t) == (sizeof(uint32_t) + sizeof(uint32_t) + sizeof(est_metadata_t) + (size_t)EST_BUFFER_SIZE + (size_t)EST_BUFFER_ALIGN_BYTES + sizeof(uint32_t) + sizeof(uint32_t)), "Size of est_ram_block_t does not match expected layout");
#elif defined(__cplusplus) && __cplusplus >= 201103L
static_assert(sizeof(est_ram_block_t) == (sizeof(uint32_t) + sizeof(uint32_t) + sizeof(est_metadata_t) + (size_t)EST_BUFFER_SIZE + (size_t)EST_BUFFER_ALIGN_BYTES + sizeof(uint32_t) + sizeof(uint32_t)), "Size of est_ram_block_t does not match expected layout");
#else
struct est_size_check { int Size_of_est_ram_block_t_does_not_match_expected_layout: (sizeof(est_ram_block_t) == (sizeof(uint32_t) + sizeof(uint32_t) + sizeof(est_metadata_t) + (size_t)EST_BUFFER_SIZE + (size_t)EST_BUFFER_ALIGN_BYTES + sizeof(uint32_t) + sizeof(uint32_t))) ? 1 : -1; };
#endif

/* Global RAM block allocated in est.c */
extern est_ram_block_t g_est_ram_block;

/* Engine prototypes */
void est_init(void);
void est_log(uint32_t id, uint32_t now_ticks, const uint8_t *payload, uint16_t payload_len);
void est_sys_timer_overflow(void);

#ifdef __cplusplus
}
#endif

#endif /* _EST_PRIV_H_ */