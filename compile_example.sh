#!/bin/bash
arm-none-eabi-as -mthumb -mthumb-interwork -mcpu=arm7tdmi example/target.s -o example/target.o
