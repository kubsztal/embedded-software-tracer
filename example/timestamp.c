#include <windows.h>
#include "timestamp.h"

void GetTimestamp(uint32_t *timestamp)
{
	static LARGE_INTEGER StartingTime, ElapsedMicroseconds, MinusTime;
	static LARGE_INTEGER Frequency;

	QueryPerformanceFrequency(&Frequency);
	QueryPerformanceCounter(&StartingTime);

	if (MinusTime.QuadPart == 0)
	{
		MinusTime.QuadPart = StartingTime.QuadPart;
	}

	// Activity to be timed
	//QueryPerformanceCounter(&EndingTime);
	//ElapsedMicroseconds.QuadPart = EndingTime.QuadPart - StartingTime.QuadPart;
	ElapsedMicroseconds.QuadPart = StartingTime.QuadPart - MinusTime.QuadPart;

	//
	// We now have the elapsed number of ticks, along with the
	// number of ticks-per-second. We use these values
	// to convert to the number of elapsed microseconds.
	// To guard against loss-of-precision, we convert
	// to microseconds *before* dividing by ticks-per-second.
	//

	ElapsedMicroseconds.QuadPart *= 1000000;
	ElapsedMicroseconds.QuadPart /= Frequency.QuadPart;

	*timestamp = (uint32_t)ElapsedMicroseconds.QuadPart;
}

