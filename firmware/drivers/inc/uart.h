#ifndef UART_H
#define UART_H

#include <stdint.h>

void UART_Init(void);
void UART2_SendByte(uint8_t data);
void UART2_SendPacket(uint8_t *buf, uint32_t len);

#endif
