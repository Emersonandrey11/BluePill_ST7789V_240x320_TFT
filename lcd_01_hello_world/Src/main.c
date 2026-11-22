/**
 ******************************************************************************
 * @file           : main.c
 * @brief          : ST7789V 240x320 TFT on an STM32F103C8T6 (Blue Pill)
 ******************************************************************************
 */

#include <stdint.h>
#include "board.h"
#include "stm32f103_regs.h"
#include "timebase.h"
#include "st7789.h"
#include "gfx.h"

int main(void)
{
    /* SystemInit() (clock.c) already set the clock to 48 MHz before main(). */

    for (;;) {
    }
}
