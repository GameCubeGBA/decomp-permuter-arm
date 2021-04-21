	.syntax unified
	.thumb
	.extern GetGameClock
	.extern ComputeDisplayTime

Return2or3ByFrameTotalParity:
        push {r4, lr}
        sub sp, #8
        bl GetGameClock
        mov r2, sp
        adds r2, #2
        add r4, sp, #4
        mov r1, sp
        adds r3, r4, #0
        bl ComputeDisplayTime
        ldrh r1, [r4]
        movs r0, #1
        ands r0, r1
        movs r1, #3
        cmp r0, #0
        bne _0801BA10
        movs r1, #2
_0801BA10:
        adds r0, r1, #0
        add sp, #8
        pop {r4}
        pop {r1}
        bx r1
