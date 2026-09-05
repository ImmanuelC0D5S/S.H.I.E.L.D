/*
 * stm32f446retx.h
 *
 * Created on: 16-Aug-2026
 * Author: imman
 */

#ifndef INC_STM32F446XX_H_
#define INC_STM32F446XX_H_

#include <stdint.h>

#define __vo volatile

/* Memory Base Addresses */

#define FLASH_BASEADDR      0x08000000U
#define SRAM1_BASEADDR      0x20000000U
#define ROM_BASEADDR        0x1FFF0000U

#define SRAM                SRAM1_BASEADDR


/* Peripheral Base Addresses */

#define PERIPH_BASE         0x40000000U
#define APB1PERIPH_BASE     PERIPH_BASE
#define APB2PERIPH_BASE     0x40010000U
#define AHB1PERIPH_BASE     0x40020000U
#define AHB2PERIPH_BASE     0x50000000U


/* Base Addresses of peripherals in AHB1 Bus */

#define GPIOA_BASEADDR      (AHB1PERIPH_BASE + 0x0000U)
#define GPIOB_BASEADDR      (AHB1PERIPH_BASE + 0x0400U)
#define GPIOC_BASEADDR      (AHB1PERIPH_BASE + 0x0800U)
#define GPIOD_BASEADDR      (AHB1PERIPH_BASE + 0x0C00U)
#define GPIOE_BASEADDR      (AHB1PERIPH_BASE + 0x1000U)
#define GPIOF_BASEADDR      (AHB1PERIPH_BASE + 0x1400U)
#define GPIOG_BASEADDR      (AHB1PERIPH_BASE + 0x1800U)
#define GPIOH_BASEADDR      (AHB1PERIPH_BASE + 0x1C00U)


/* Base Addresses of peripherals in APB1 Bus */

#define SPI2_BASEADDR       (APB1PERIPH_BASE + 0x3800U)
#define SPI3_BASEADDR       (APB1PERIPH_BASE + 0x3C00U)

#define UART4_BASEADDR      (APB1PERIPH_BASE + 0x4C00U)
#define UART5_BASEADDR      (APB1PERIPH_BASE + 0x5000U)

#define I2C1_BASEADDR       (APB1PERIPH_BASE + 0x5400U)
#define I2C2_BASEADDR       (APB1PERIPH_BASE + 0x5800U)
#define I2C3_BASEADDR       (APB1PERIPH_BASE + 0x5C00U)

#define USART2_BASEADDR     (APB1PERIPH_BASE + 0x4400U)
#define USART3_BASEADDR     (APB1PERIPH_BASE + 0x4800U)


/* Base Addresses of peripherals in APB2 Bus */

#define USART6_BASEADDR     (APB2PERIPH_BASE + 0x1000U)
#define USART1_BASEADDR     (APB2PERIPH_BASE + 0x1400U)

#define SPI1_BASEADDR       (APB2PERIPH_BASE + 0x3000U)

#define SYSCFG_BASEADDR     (APB2PERIPH_BASE + 0x3800U)
#define EXTI_BASEADDR       (APB2PERIPH_BASE + 0x3C00U)


/* GPIO Register Definition */

typedef struct
{
    __vo uint32_t MODER;       /* 0x00 */
    __vo uint32_t OTYPER;      /* 0x04 */
    __vo uint32_t OSPEEDR;     /* 0x08 */
    __vo uint32_t PUPDR;       /* 0x0C */
    __vo uint32_t IDR;         /* 0x10 */
    __vo uint32_t ODR;         /* 0x14 */
    __vo uint32_t BSRR;        /* 0x18 */
    __vo uint32_t LCKR;        /* 0x1C */
    __vo uint32_t AFR[2];      /* 0x20, 0x24 */

} GPIO_RegDef_t;


/* GPIO Peripheral Definitions */

#define GPIOA    ((GPIO_RegDef_t*)GPIOA_BASEADDR)
#define GPIOB    ((GPIO_RegDef_t*)GPIOB_BASEADDR)
#define GPIOC    ((GPIO_RegDef_t*)GPIOC_BASEADDR)
#define GPIOD    ((GPIO_RegDef_t*)GPIOD_BASEADDR)
#define GPIOE    ((GPIO_RegDef_t*)GPIOE_BASEADDR)
#define GPIOF    ((GPIO_RegDef_t*)GPIOF_BASEADDR)
#define GPIOG    ((GPIO_RegDef_t*)GPIOG_BASEADDR)
#define GPIOH    ((GPIO_RegDef_t*)GPIOH_BASEADDR)


/* RCC Base Address */

#define RCC_BASEADDR        0x40023800U


/* RCC Register Definition */

typedef struct
{
    __vo uint32_t RCC_CR;          /* 0x00 */
    __vo uint32_t PLLCFGR;         /* 0x04 */
    __vo uint32_t CFGR;            /* 0x08 */
    __vo uint32_t CIR;             /* 0x0C */

    __vo uint32_t AHB1RSTR;        /* 0x10 */
    __vo uint32_t AHB2RSTR;        /* 0x14 */
    __vo uint32_t AHB3RSTR;        /* 0x18 */
    uint32_t RESERVED0;            /* 0x1C */

    __vo uint32_t APB1RSTR;        /* 0x20 */
    __vo uint32_t APB2RSTR;        /* 0x24 */
    uint32_t RESERVED1[2];         /* 0x28 - 0x2C */

    __vo uint32_t AHB1ENR;         /* 0x30 */
    __vo uint32_t AHB2ENR;         /* 0x34 */
    __vo uint32_t AHB3ENR;         /* 0x38 */
    uint32_t RESERVED2;            /* 0x3C */

    __vo uint32_t APB1ENR;         /* 0x40 */
    __vo uint32_t APB2ENR;         /* 0x44 */
    uint32_t RESERVED3[2];         /* 0x48 - 0x4C */

    __vo uint32_t AHB1LPENR;       /* 0x50 */
    __vo uint32_t AHB2LPENR;       /* 0x54 */
    __vo uint32_t AHB3LPENR;       /* 0x58 */
    uint32_t RESERVED4;            /* 0x5C */

    __vo uint32_t APB1LPENR;       /* 0x60 */
    __vo uint32_t APB2LPENR;       /* 0x64 */
    uint32_t RESERVED5[2];         /* 0x68 - 0x6C */

    __vo uint32_t BDCR;             /* 0x70 */
    __vo uint32_t CSR;              /* 0x74 */
    uint32_t RESERVED6[2];          /* 0x78 - 0x7C */

    __vo uint32_t SSCGR;            /* 0x80 */
    __vo uint32_t PLLI2SCFGR;       /* 0x84 */
    __vo uint32_t PLLSAICFGR;       /* 0x88 */
    __vo uint32_t DCKCFGR;          /* 0x8C */
    __vo uint32_t CKGATENR;         /* 0x90 */
    __vo uint32_t DCKCFGR2;        /* 0x94 */

} RCC_RegDef_t;


/* RCC Peripheral Definition */

#define RCC    ((RCC_RegDef_t*)RCC_BASEADDR)


/* Clock Enable Macros for GPIO peripherals */

#define GPIOA_PCLK_EN()    (RCC->AHB1ENR |= (1U << 0))
#define GPIOB_PCLK_EN()    (RCC->AHB1ENR |= (1U << 1))
#define GPIOC_PCLK_EN()    (RCC->AHB1ENR |= (1U << 2))
#define GPIOD_PCLK_EN()    (RCC->AHB1ENR |= (1U << 3))
#define GPIOE_PCLK_EN()    (RCC->AHB1ENR |= (1U << 4))
#define GPIOF_PCLK_EN()    (RCC->AHB1ENR |= (1U << 5))
#define GPIOG_PCLK_EN()    (RCC->AHB1ENR |= (1U << 6))
#define GPIOH_PCLK_EN()    (RCC->AHB1ENR |= (1U << 7))


/* Clock Enable Macros for I2C peripherals */

#define I2C1_PCLK_EN()     (RCC->APB1ENR |= (1U << 21))
#define I2C2_PCLK_EN()     (RCC->APB1ENR |= (1U << 22))
#define I2C3_PCLK_EN()     (RCC->APB1ENR |= (1U << 23))


/* Clock Enable Macros for SPI peripherals */

#define SPI1_PCLK_EN()     (RCC->APB2ENR |= (1U << 12))
#define SPI2_PCLK_EN()     (RCC->APB1ENR |= (1U << 14))
#define SPI3_PCLK_EN()     (RCC->APB1ENR |= (1U << 15))


/* Clock Enable Macros for USART/UART peripherals */

#define USART1_PCLK_EN()   (RCC->APB2ENR |= (1U << 4))
#define USART2_PCLK_EN()   (RCC->APB1ENR |= (1U << 17))
#define USART3_PCLK_EN()   (RCC->APB1ENR |= (1U << 18))
#define UART4_PCLK_EN()    (RCC->APB1ENR |= (1U << 19))
#define UART5_PCLK_EN()    (RCC->APB1ENR |= (1U << 20))
#define USART6_PCLK_EN()   (RCC->APB2ENR |= (1U << 5))


/* Clock Disable Macros for GPIO peripherals */

#define GPIOA_PCLK_DI()    (RCC->AHB1ENR &= ~(1U << 0))
#define GPIOB_PCLK_DI()    (RCC->AHB1ENR &= ~(1U << 1))
#define GPIOC_PCLK_DI()    (RCC->AHB1ENR &= ~(1U << 2))
#define GPIOD_PCLK_DI()    (RCC->AHB1ENR &= ~(1U << 3))
#define GPIOE_PCLK_DI()    (RCC->AHB1ENR &= ~(1U << 4))
#define GPIOF_PCLK_DI()    (RCC->AHB1ENR &= ~(1U << 5))
#define GPIOG_PCLK_DI()    (RCC->AHB1ENR &= ~(1U << 6))
#define GPIOH_PCLK_DI()    (RCC->AHB1ENR &= ~(1U << 7))


/* Clock Disable Macros for I2C peripherals */

#define I2C1_PCLK_DI()     (RCC->APB1ENR &= ~(1U << 21))
#define I2C2_PCLK_DI()     (RCC->APB1ENR &= ~(1U << 22))
#define I2C3_PCLK_DI()     (RCC->APB1ENR &= ~(1U << 23))


/* Clock Disable Macros for SPI peripherals */

#define SPI1_PCLK_DI()     (RCC->APB2ENR &= ~(1U << 12))
#define SPI2_PCLK_DI()     (RCC->APB1ENR &= ~(1U << 14))
#define SPI3_PCLK_DI()     (RCC->APB1ENR &= ~(1U << 15))


/* Clock Disable Macros for USART/UART peripherals */

#define USART1_PCLK_DI()   (RCC->APB2ENR &= ~(1U << 4))
#define USART2_PCLK_DI()   (RCC->APB1ENR &= ~(1U << 17))
#define USART3_PCLK_DI()   (RCC->APB1ENR &= ~(1U << 18))
#define UART4_PCLK_DI()    (RCC->APB1ENR &= ~(1U << 19))
#define UART5_PCLK_DI()    (RCC->APB1ENR &= ~(1U << 20))
#define USART6_PCLK_DI()   (RCC->APB2ENR &= ~(1U << 5))

/* EXTI Base */
#define EXTI_BASEADDR (APB2PERIPH_BASE + 0x3C00U)

/* SYSCFG already defined — good */

/* EXTI Register structure */
typedef struct {
    __vo uint32_t IMR;    // 0x00
    __vo uint32_t EMR;    // 0x04
    __vo uint32_t RTSR;   // 0x08
    __vo uint32_t FTSR;   // 0x0C
    __vo uint32_t SWIER;  // 0x10
    __vo uint32_t PR;     // 0x14
} EXTI_RegDef_t;

/* SYSCFG Register structure */
typedef struct
{
    __vo uint32_t MEMRMP;    // 0x00
    __vo uint32_t PMC;       // 0x04
    __vo uint32_t EXTICR[4]; // 0x08-0x14
    uint32_t RESERVED[2];    // 0x18-0x1C
    __vo uint32_t CMPCR;     // 0x20
} SYSCFG_RegDef_t;

/* Pointer macros */
#define EXTI   ((EXTI_RegDef_t*)EXTI_BASEADDR)
#define SYSCFG ((SYSCFG_RegDef_t*)SYSCFG_BASEADDR)

/* SYSCFG clock enable */
#define SYSCFG_PCLK_EN() (RCC->APB2ENR |= (1 << 14))


/* TIM2 Base */
#define TIM2_BASEADDR (APB1PERIPH_BASE + 0x0000U)

/* TIM2 Register struct */
typedef struct {
    __vo uint32_t CR1;   // 0x00
    __vo uint32_t CR2;   // 0x04
    __vo uint32_t SMCR;  // 0x08
    __vo uint32_t DIER;  // 0x0C
    __vo uint32_t SR;    // 0x10
    __vo uint32_t EGR;   // 0x14
    __vo uint32_t CCMR1; // 0x18
    __vo uint32_t CCMR2; // 0x1C
    __vo uint32_t CCER;  // 0x20
    __vo uint32_t CNT;   // 0x24
    __vo uint32_t PSC;   // 0x28
    __vo uint32_t ARR;   // 0x2C
    __vo uint32_t RCR;   // 0x30 — reserved on TIM2 but must be here to keep offset
    __vo uint32_t CCR1;  // 0x34
    __vo uint32_t CCR2;  // 0x38
    __vo uint32_t CCR3;  // 0x3C
    __vo uint32_t CCR4;  // 0x40
} TIM2_RegDef_t;
#define TIM2 ((TIM2_RegDef_t*)TIM2_BASEADDR)

/* TIM2 clock enable */
#define TIM2_PCLK_EN() (RCC->APB1ENR |= (1 << 0))

/* USART register struct*/
typedef struct {
    __vo uint32_t SR;   // 0x00
    __vo uint32_t DR;   // 0x04
    __vo uint32_t BRR;  // 0x08
    __vo uint32_t CR1;  // 0x0C
    __vo uint32_t CR2;  // 0x10
    __vo uint32_t CR3;  // 0x14
    __vo uint32_t GTPR;    // 0x18
} USART2_RegDef_t;

#define USART2 ((USART2_RegDef_t*)USART2_BASEADDR)

#define NVIC_ISER0 (*(volatile uint32_t *)0xE000E100)
#define NVIC_ISER1 (*(volatile uint32_t *)0xE000E104)

#endif /* INC_STM32F446XX_H_ */
