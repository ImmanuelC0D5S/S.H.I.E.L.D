#include "stm32f446xx.h"
#include "gpio.h"
#include "tim2.h"
#include "uart.h"
#include <stdint.h>

#define NVIC_ISER1 (*(volatile uint32_t *)0xE000E104)

volatile uint32_t pir_timestamp = 0;
volatile uint8_t motion_detected = 0;

void EXTI1_IRQHandler(void)
{
    if (EXTI->PR & (1 << 1))
    {
        pir_timestamp = TIM2->CNT;
        motion_detected = 1;
        EXTI->PR |= (1 << 1);
    }
}

int main(void)
{
    GPIO_Init();
    // Debug — read back registers
    // Send each value over UART as raw bytes
    TIM2_Init();
    UART_Init();
    PWM_Init();

    uint8_t test[] = "INIT DONE\r\n";
    UART2_SendPacket(test, 11);

    while(1)
    {
        if (motion_detected)
        {
            motion_detected = 0;
            PWM_BuzzerOn();

            uint8_t packet[7];
            packet[0] = 0xAA;                      // start byte
            packet[1] = 0x01;                      // event: motion
            packet[2] = (pir_timestamp >> 24) & 0xFF;
            packet[3] = (pir_timestamp >> 16) & 0xFF;
            packet[4] = (pir_timestamp >> 8)  & 0xFF;
            packet[5] = (pir_timestamp)        & 0xFF;
            packet[6] = packet[1] ^ packet[2] ^ packet[3] ^ packet[4] ^ packet[5]; // checksum

            UART2_SendPacket(packet, 7);
            PWM_BuzzerOff();
        }
    }
}
