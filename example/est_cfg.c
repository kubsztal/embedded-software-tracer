#include "est.h"
#include "est_priv.h"
#include <string.h>

#define _EST_CFG_C_ (0x5F8F6E1AuL)
#if _EST_CFG_H_ != _EST_CFG_C_
#error "Hash mismatch between est_cfg.h and est_cfg.c"
#endif

void TRC_Init(void) {
    est_init();
}

void TimerOverflow(void) {
    est_sys_timer_overflow();
}

void est_pack_u8a5(uint8_t *buf, const uint8_t *d1) {
    uint32_t idx = 0;
    memcpy(&buf[idx], d1, sizeof(uint8_t) * 5u);
    idx += sizeof(uint8_t) * 5u;
}

void est_pack_u8_u8a3(uint8_t *buf, uint8_t d1, const uint8_t *d2) {
    uint32_t idx = 0;
    memcpy(&buf[idx], &d1, sizeof(uint8_t));
    idx += sizeof(uint8_t);
    memcpy(&buf[idx], d2, sizeof(uint8_t) * 3u);
    idx += sizeof(uint8_t) * 3u;
}

void est_pack_u32_f64(uint8_t *buf, uint32_t d1, double d2) {
    uint32_t idx = 0;
    memcpy(&buf[idx], &d1, sizeof(uint32_t));
    idx += sizeof(uint32_t);
    memcpy(&buf[idx], &d2, sizeof(double));
    idx += sizeof(double);
}

void est_pack_b5_b12_b3(uint8_t *buf, uint8_t d1, uint16_t d2, uint8_t d3) {
    uint32_t idx = 0;
    uint16_t bf0 = ((uint16_t)(d1) & 0x1Fu) | (((uint16_t)(d2) & 0x7FFu) << 5u);
    memcpy(&buf[idx], &bf0, sizeof(uint16_t));
    idx += sizeof(uint16_t);
    uint8_t bf1 = ((uint8_t)((d2 >> 11u)) & 0x1u) | (((uint8_t)(d3) & 0x7u) << 1u);
    memcpy(&buf[idx], &bf1, sizeof(uint8_t));
    idx += sizeof(uint8_t);
}

void est_pack_s16(uint8_t *buf, int16_t d1) {
    uint32_t idx = 0;
    memcpy(&buf[idx], &d1, sizeof(int16_t));
    idx += sizeof(int16_t);
}

void est_pack_u8(uint8_t *buf, uint8_t ic) {
    uint32_t idx = 0;
    memcpy(&buf[idx], &ic, sizeof(uint8_t));
    idx += sizeof(uint8_t);
}

void est_pack_u8_u8_u8(uint8_t *buf, uint8_t i, uint8_t d1, uint8_t d2) {
    uint32_t idx = 0;
    memcpy(&buf[idx], &i, sizeof(uint8_t));
    idx += sizeof(uint8_t);
    memcpy(&buf[idx], &d1, sizeof(uint8_t));
    idx += sizeof(uint8_t);
    memcpy(&buf[idx], &d2, sizeof(uint8_t));
    idx += sizeof(uint8_t);
}

