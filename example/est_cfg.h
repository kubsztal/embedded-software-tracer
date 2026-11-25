#ifndef _EST_CFG_H_
#define _EST_CFG_H_ (0x5F8F6E1AuL)

#include <stdint.h>
#include <stdbool.h>
#include "timestamp.h"
#include "critical_sections.h"

#ifdef __cplusplus
extern "C" {
#endif

#define EST_BUFFER_SIZE         (1000u)
#define EST_BUFFER_ALIGN_BYTES  (0u)
/* 32-bit fold of config.json's real 256-bit SHA-256 hash (see config.json's
   own short_hash field, which generate_est_cfg_files.py verifies this matches
   before generating) -- not the hash itself, hence 'short'. */
#define EST_SHORT_CONFIG_HASH   (0x5F8F6E1AuL)
#define EST_TRACE_ID_BYTES      (2u)
#define EST_BUFFER_TYPE         (EST_BF_LINEAR)
#define EST_TIMESTAMP_STORAGE   (EST_TS_FULL_4B)
#define EST_TS_STORAGE_BYTES    (4u)
#define EST_TS_CNT_BYTES        (4u)
#define EST_TS_CNT_MASK         ((uint32_t)0xFFFFFFFFu)
#define EST_ID_SYNC             ((uint16_t)0xFFFFu)

/* Public Application Endpoints */
void TRC_Init(void);
void TimerOverflow(void);

void est_pack_u8a5(uint8_t *buf, const uint8_t *d1);
void est_pack_u8_u8a3(uint8_t *buf, uint8_t d1, const uint8_t *d2);
void est_pack_u32_f64(uint8_t *buf, uint32_t d1, double d2);
void est_pack_b5_b12_b3(uint8_t *buf, uint8_t d1, uint16_t d2, uint8_t d3);
void est_pack_s16(uint8_t *buf, int16_t d1);
void est_pack_u8(uint8_t *buf, uint8_t ic);
void est_pack_u8_u8_u8(uint8_t *buf, uint8_t i, uint8_t d1, uint8_t d2);

/* Declared here (not only in the internal est_priv.h) because the
   trace macros below expand to calls to it from application code,
   which is not expected to include that internal header. */
void est_log(uint32_t id, uint32_t now_ticks, const uint8_t *payload, uint16_t payload_len);

/* Trace Point Access Macros */
#define TRC_EV_Tracepoint0(...) do {} while ((void)0, 0)
#define TRC_SIG_Tracepoint1_signal_Send(...) do {} while ((void)0, 0)
#define TRC_SIG_Tracepoint1_signal_Receive(...) do {} while ((void)0, 0)
#define TRC_TSK_Tracepoint2_task_Start() do {} while ((void)0, 0)
#define TRC_TSK_Tracepoint2_task_Activate() do {} while ((void)0, 0)
#define TRC_TSK_Tracepoint2_task_Preempt() do {} while ((void)0, 0)
#define TRC_TSK_Tracepoint2_task_Terminate() do {} while ((void)0, 0)
#define TRC_TSK_Tracepoint2_task_Wait() do {} while ((void)0, 0)
#define TRC_TSK_Tracepoint2_task_Release() do {} while ((void)0, 0)
#define TRC_ISR_Tracepoint3_isr_Begin() do {} while ((void)0, 0)
#define TRC_ISR_Tracepoint3_isr_End() do {} while ((void)0, 0)
#define TRC_RT_Tracepoint4_routine_Begin(...) do {} while ((void)0, 0)
#define TRC_RT_Tracepoint4_routine_End(...) do {} while ((void)0, 0)
#define TRC_HSM_Tracepoint5_hsm_Init() do {} while ((void)0, 0)
#define TRC_HSM_Tracepoint5_hsm_Entry() do {} while ((void)0, 0)
#define TRC_HSM_Tracepoint5_hsm_Exit() do {} while ((void)0, 0)
#define TRC_HSM_Tracepoint5_hsm_Event() do {} while ((void)0, 0)
#define TRC_HSM_Tracepoint5_hsm_Timer() do {} while ((void)0, 0)
#define TRC_HSM_Tracepoint5_hsm_Transition() do {} while ((void)0, 0)
#define TRC_EV_Tracepoint6(...) do {} while ((void)0, 0)
#define TRC_SIG_Tracepoint7_signal_Send() do {} while ((void)0, 0)
#define TRC_SIG_Tracepoint7_signal_Receive() do {} while ((void)0, 0)
#define TRC_TSK_Tracepoint8_task_Start(...) do {} while ((void)0, 0)
#define TRC_TSK_Tracepoint8_task_Activate(...) do {} while ((void)0, 0)
#define TRC_TSK_Tracepoint8_task_Preempt(...) do {} while ((void)0, 0)
#define TRC_TSK_Tracepoint8_task_Terminate(...) do {} while ((void)0, 0)
#define TRC_TSK_Tracepoint8_task_Wait(...) do {} while ((void)0, 0)
#define TRC_TSK_Tracepoint8_task_Release(...) do {} while ((void)0, 0)
#define TRC_ISR_Tracepoint9_isr_Begin() do {} while ((void)0, 0)
#define TRC_ISR_Tracepoint9_isr_End() do {} while ((void)0, 0)
#define TRC_RT_Tracepoint10_routine_Begin() do {} while ((void)0, 0)
#define TRC_RT_Tracepoint10_routine_End() do {} while ((void)0, 0)
#define TRC_HSM_Tracepoint11_hsm_Init(...) do {} while ((void)0, 0)
#define TRC_HSM_Tracepoint11_hsm_Entry(...) do {} while ((void)0, 0)
#define TRC_HSM_Tracepoint11_hsm_Exit(...) do {} while ((void)0, 0)
#define TRC_HSM_Tracepoint11_hsm_Event(...) do {} while ((void)0, 0)
#define TRC_HSM_Tracepoint11_hsm_Timer(...) do {} while ((void)0, 0)
#define TRC_HSM_Tracepoint11_hsm_Transition(...) do {} while ((void)0, 0)
#define TRC_FIFO_Trc001_signal_Send() do { \
                                           uint32_t timestamp; \
                                           GetTimestamp(&timestamp); \
                                           est_log(((uint16_t)0xB413u), timestamp, NULL, ((uint16_t)0u)); \
                                         } while ((void)0, 0) /* Core 0 */
#define TRC_FIFO_Trc001_signal_Receive() do { \
                                              uint32_t timestamp; \
                                              GetTimestamp(&timestamp); \
                                              est_log(((uint16_t)0xB67Au), timestamp, NULL, ((uint16_t)0u)); \
                                            } while ((void)0, 0) /* Core 0 */
#define TRC_FILO_Trc002_routine_Begin(c) do { \
                                              uint32_t timestamp; \
                                              GetTimestamp(&timestamp); \
                                              uint8_t pbuf[1]; \
                                              est_pack_u8(pbuf, c); \
                                              est_log(((uint16_t)0x71F6u), timestamp, pbuf, (uint16_t)sizeof(pbuf)); \
                                            } while ((void)0, 0)
#define TRC_FILO_Trc002_routine_End(c) do { \
                                            uint32_t timestamp; \
                                            GetTimestamp(&timestamp); \
                                            uint8_t pbuf[1]; \
                                            est_pack_u8(pbuf, c); \
                                            est_log(((uint16_t)0xF457u), timestamp, pbuf, (uint16_t)sizeof(pbuf)); \
                                          } while ((void)0, 0)

#ifdef __cplusplus
}
#endif

#endif /* _EST_CFG_H_ */
