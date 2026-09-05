/*
 * tim2.c
 *
 *  Created on: 22-Aug-2026
 *      Author: imman
 */


#include "stm32f446xx.h"
#include <stdint.h>

void TIM2_Init()
{
	TIM2_PCLK_EN();

	TIM2->PSC = 16000 - 1;

	TIM2->ARR = 0XFFFFFFFF;

	TIM2->CR1 |= (1 << 0);
}

void PWM_Init(void)
{
    // PA0 alternate function AF1 (TIM2_CH1)
    GPIOA_PCLK_EN();
    GPIOA->MODER  &= ~(3 << 0);
    GPIOA->MODER  |=  (2 << 0);   // alternate function
    GPIOA->AFR[0] &= ~(0xF << 0);
    GPIOA->AFR[0] |=  (1 << 0);   // AF1 = TIM2

    // TIM2 CH1 PWM mode 1
    TIM2->CCMR1 &= ~(7 << 4);
    TIM2->CCMR1 |=  (6 << 4);     // OC1M = 110 (PWM mode 1)
    TIM2->CCMR1 |=  (1 << 3);     // OC1PE — preload enable

    // Set frequency — 1kHz buzz
    TIM2->PSC = 16 - 1;
    TIM2->ARR = 1000 - 1;

    // Duty cycle — 50%
    TIM2->CCR1 = 500;

    // Enable CH1 output
    TIM2->CCER |= (1 << 0);

    // Enable TIM2
    TIM2->CR1 |= (1 << 0);
}

void PWM_BuzzerOn(void)  { TIM2->CCR1 = 500; }
void PWM_BuzzerOff(void) { TIM2->CCR1 = 0;   }
