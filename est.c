#include "est_priv.h"
#include <string.h>

est_ram_block_t g_est_ram_block;

#if (EST_TIMESTAMP_STORAGE == EST_TS_ULEB128)
static uint32_t g_last_timestamp = 0u;
#endif

/* -------------------------------------------------------------------------- */
/* Low-Level Buffer Writers                                                   */
/* -------------------------------------------------------------------------- */
#if (EST_BUFFER_TYPE == EST_BF_LINEAR)
static void est_write_linear(const uint8_t *src, uint32_t len) {
    (void)memcpy(&g_est_ram_block.buffer[g_est_ram_block.meta.write_idx], src, (size_t)len);
    g_est_ram_block.meta.write_idx += len;
}
#elif (EST_BUFFER_TYPE == EST_BF_CIRCULAR)
static void est_write_circular(const uint8_t *src, uint32_t len) {
    if (len > EST_BUFFER_SIZE) {
        src = &src[len - EST_BUFFER_SIZE];
        len = EST_BUFFER_SIZE;
    }

    uint32_t to_end = EST_BUFFER_SIZE - g_est_ram_block.meta.write_idx;
    if (len <= to_end) {
        (void)memcpy(&g_est_ram_block.buffer[g_est_ram_block.meta.write_idx], src, (size_t)len);
        g_est_ram_block.meta.write_idx += len;
        if (g_est_ram_block.meta.write_idx == EST_BUFFER_SIZE) {
            g_est_ram_block.meta.write_idx = 0u;
            g_est_ram_block.meta.is_wrapped = 1u;
        }
    } else {
        (void)memcpy(&g_est_ram_block.buffer[g_est_ram_block.meta.write_idx], src, (size_t)to_end);
        uint32_t rem = len - to_end;
        (void)memcpy(&g_est_ram_block.buffer[0], &src[to_end], (size_t)rem);
        g_est_ram_block.meta.write_idx = rem;
        g_est_ram_block.meta.is_wrapped = 1u;
    }
}
#endif

static inline uint32_t est_encode_ts(uint8_t *ts_buf, uint32_t now_ticks) {
#if (EST_TIMESTAMP_STORAGE == EST_TS_ULEB128)
    uint32_t diff = (now_ticks - g_last_timestamp) & EST_TS_CNT_MASK;
    uint32_t len = 0u;
    do {
        uint8_t byte = (uint8_t)(diff & 0x7Fu);
        diff >>= 7u;
        if (diff > 0u) {
            byte |= 0x80u;
        }
        ts_buf[len++] = byte;
    } while (diff > 0u);
    return len;
#elif (EST_TIMESTAMP_STORAGE == EST_TS_FULL_2B)
    ts_buf[0] = (uint8_t)(now_ticks & 0xFFu);
    ts_buf[1] = (uint8_t)((now_ticks >> 8u) & 0xFFu);
    return 2u;
#elif (EST_TIMESTAMP_STORAGE == EST_TS_FULL_3B)
    ts_buf[0] = (uint8_t)(now_ticks & 0xFFu);
    ts_buf[1] = (uint8_t)((now_ticks >> 8u) & 0xFFu);
    ts_buf[2] = (uint8_t)((now_ticks >> 16u) & 0xFFu);
    return 3u;
#elif (EST_TIMESTAMP_STORAGE == EST_TS_FULL_4B)
    ts_buf[0] = (uint8_t)(now_ticks & 0xFFu);
    ts_buf[1] = (uint8_t)((now_ticks >> 8u) & 0xFFu);
    ts_buf[2] = (uint8_t)((now_ticks >> 16u) & 0xFFu);
    ts_buf[3] = (uint8_t)((now_ticks >> 24u) & 0xFFu);
    return 4u;
#else
#error "Invalid EST_TIMESTAMP_STORAGE configuration"
#endif
}

/* -------------------------------------------------------------------------- */
/* Core Engine Interface                                                      */
/* -------------------------------------------------------------------------- */
void est_init(void) {
    g_est_ram_block.magic_start     = EST_RAM_MAGIC_START;
    g_est_ram_block.magic_start_inv = EST_RAM_MAGIC_START_INV;
    g_est_ram_block.meta.write_idx  = 0u;
    g_est_ram_block.meta.is_wrapped = 0u;
    g_est_ram_block.meta.is_stopped = 0u;
    g_est_ram_block.meta.reserved[0] = 0u;
    g_est_ram_block.meta.reserved[1] = 0u;
    g_est_ram_block.magic_end_inv   = EST_RAM_MAGIC_END_INV;
    g_est_ram_block.magic_end       = EST_RAM_MAGIC_END;

#if (EST_TIMESTAMP_STORAGE == EST_TS_ULEB128)
    g_last_timestamp = 0u;
#endif
}

void est_log(uint32_t id, uint32_t now_ticks, const uint8_t *payload, uint16_t payload_len) {
#if (EST_BUFFER_TYPE == EST_BF_LINEAR)
    if (g_est_ram_block.meta.is_stopped != 0u) {
        return;
    }
#endif

    /* 1. ID Serialization */
    uint8_t id_buf[4];
#if (EST_TRACE_ID_BYTES == 1u)
    id_buf[0] = (uint8_t)(id & 0xFFu);
#elif (EST_TRACE_ID_BYTES == 2u)
    id_buf[0] = (uint8_t)(id & 0xFFu);
    id_buf[1] = (uint8_t)((id >> 8u) & 0xFFu);
#elif (EST_TRACE_ID_BYTES == 3u)
    id_buf[0] = (uint8_t)(id & 0xFFu);
    id_buf[1] = (uint8_t)((id >> 8u) & 0xFFu);
    id_buf[2] = (uint8_t)((id >> 16u) & 0xFFu);
#elif (EST_TRACE_ID_BYTES == 4u)
    id_buf[0] = (uint8_t)(id & 0xFFu);
    id_buf[1] = (uint8_t)((id >> 8u) & 0xFFu);
    id_buf[2] = (uint8_t)((id >> 16u) & 0xFFu);
    id_buf[3] = (uint8_t)((id >> 24u) & 0xFFu);
#else
#error "Invalid EST_TRACE_ID_BYTES configuration"
#endif

    /* 2. Timestamp Serialization */
    uint8_t ts_buf[5];
    uint32_t ts_len = est_encode_ts(ts_buf, now_ticks);

    uint32_t rec_len = (uint32_t)EST_TRACE_ID_BYTES + ts_len + (uint32_t)payload_len;

#if (EST_BUFFER_TYPE == EST_BF_LINEAR)
    /* If record does not fit in remaining linear space, permanently halt tracing */
    if ((g_est_ram_block.meta.write_idx + rec_len) > EST_BUFFER_SIZE) {
        g_est_ram_block.meta.is_stopped = 1u;
        return;
    }

#if (EST_TIMESTAMP_STORAGE == EST_TS_ULEB128)
    g_last_timestamp = now_ticks;
#endif

    est_write_linear(id_buf, (uint32_t)EST_TRACE_ID_BYTES);
    est_write_linear(ts_buf, ts_len);
    if ((payload != NULL) && (payload_len > 0u)) {
        est_write_linear(payload, (uint32_t)payload_len);
    }

#elif (EST_BUFFER_TYPE == EST_BF_CIRCULAR)
#if (EST_TIMESTAMP_STORAGE == EST_TS_ULEB128)
    g_last_timestamp = now_ticks;
#endif

    est_write_circular(id_buf, (uint32_t)EST_TRACE_ID_BYTES);
    est_write_circular(ts_buf, ts_len);
    if ((payload != NULL) && (payload_len > 0u)) {
        est_write_circular(payload, (uint32_t)payload_len);
    }
#else
#error "Unsupported EST_BUFFER_TYPE configuration"
#endif
}

void est_sys_timer_overflow(void) {
    est_log((uint32_t)EST_ID_SYNC, 0u, NULL, 0u);
}