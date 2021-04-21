unsigned int GetGameClock(void);
unsigned char ComputeDisplayTime();

int Return2or3ByFrameTotalParity(void)
{
    int retVal;
    unsigned short hours;
    unsigned short minutes;
    unsigned short seconds;

    ComputeDisplayTime(GetGameClock(),&hours,&minutes,&seconds);
    if ((seconds & 1) == 0) {
        retVal = 2;
    }
    else {
        retVal = 3;
    }
    return retVal;
}
