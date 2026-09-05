################################################################################
# Automatically-generated file. Do not edit!
# Toolchain: GNU Tools for STM32 (14.3.rel1)
################################################################################

# Add inputs and outputs from these tool invocations to the build variables 
C_SRCS += \
../drivers/src/gpio.c \
../drivers/src/tim2.c \
../drivers/src/uart.c 

OBJS += \
./drivers/src/gpio.o \
./drivers/src/tim2.o \
./drivers/src/uart.o 

C_DEPS += \
./drivers/src/gpio.d \
./drivers/src/tim2.d \
./drivers/src/uart.d 


# Each subdirectory must supply rules for building sources it contributes
drivers/src/%.o drivers/src/%.su drivers/src/%.cyclo: ../drivers/src/%.c drivers/src/subdir.mk
	arm-none-eabi-gcc "$<" -mcpu=cortex-m4 -std=gnu11 -g3 -DDEBUG -DSTM32 -DSTM32F4 -DSTM32F446RETx -c -I"C:/Users/imman/STM32CubeIDE/workspace_2.1.1/S.H.I.E.L.D/drivers/inc" -I"C:/Users/imman/STM32CubeIDE/workspace_2.1.1/S.H.I.E.L.D/drivers/src" -O0 -ffunction-sections -fdata-sections -Wall -fstack-usage -fcyclomatic-complexity -MMD -MP -MF"$(@:%.o=%.d)" -MT"$@" --specs=nano.specs -mfpu=fpv4-sp-d16 -mfloat-abi=hard -mthumb -o "$@"

clean: clean-drivers-2f-src

clean-drivers-2f-src:
	-$(RM) ./drivers/src/gpio.cyclo ./drivers/src/gpio.d ./drivers/src/gpio.o ./drivers/src/gpio.su ./drivers/src/tim2.cyclo ./drivers/src/tim2.d ./drivers/src/tim2.o ./drivers/src/tim2.su ./drivers/src/uart.cyclo ./drivers/src/uart.d ./drivers/src/uart.o ./drivers/src/uart.su

.PHONY: clean-drivers-2f-src

