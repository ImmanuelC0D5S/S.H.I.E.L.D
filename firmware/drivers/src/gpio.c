/*
 * gpio.c
 *
 *  Created on: 22-Aug-2026
 *      Author: imman
 */

#include "stm32f446xx.h"
#include <stdint.h>
#define NVIC_ISER1  (*(volatile uint32_t *)0xE000E104)

void GPIO_Init(void)
{
    GPIOA_PCLK_EN();

    SYSCFG_PCLK_EN();

    // PA1 as input
    GPIOA->MODER &= ~(3 << 2);

    // Pull-down on PA1
    GPIOA->PUPDR &= ~(3 << 2);
    GPIOA->PUPDR |=  (2 << 2);

    // EXTICR[0] — pin 1 → Port A = 0000
    SYSCFG->EXTICR[0] &= ~(0xF << 4);

    // Unmask EXTI1
    EXTI->IMR |= (1 << 1);

    // Rising edge
    EXTI->RTSR |= (1 << 1);

    // EXTI1 = IRQ7 → ISER0 bit 7
    NVIC_ISER0 |= (1 << 7);
}
