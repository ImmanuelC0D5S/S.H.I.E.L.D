/*
 * uart.c
 *
 *  Created on: 22-Aug-2026
 *      Author: imman
 */

#define UART_C_

#include "stm32f446xx.h"
#include <stdint.h>

void UART2_SendByte(uint8_t data)
{
    while (!(USART2->SR & (1 << 7)));  // wait until TX buffer empty
    USART2->DR = data;
}

void UART2_SendPacket(uint8_t *buf, uint32_t len)
{
    for (uint32_t i = 0; i < len; i++)
    {
        UART2_SendByte(buf[i]);
    }

}


void UART_Init(void)
{
    USART2_PCLK_EN();
    GPIOA_PCLK_EN();

    // PA2 alternate function — clear first
    GPIOA->MODER &= ~(3U << 4);
    GPIOA->MODER |=  (2U << 4);

    // Clear AFR bits for PA2 first
    GPIOA->AFR[0] &= ~(0xF << 8);
    GPIOA->AFR[0] |=  (7 << 8);

    USART2->BRR = 1667;  // 16MHz / 9600
    USART2->CR1 |= (1 << 3);
    USART2->CR1 |= (1 << 13);
}

