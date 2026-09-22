"""Global constants"""

import os

TEST_MODE: bool = False

TECHNICAL_SUPPORT_SERVER: int = 0
AI_SIGNAL_CHANNEL: int = 0
TEST_SERVER: int = 0

def init(test_mode: bool) -> None:
    global TEST_MODE, TECHNICAL_SUPPORT_SERVER, AI_SIGNAL_CHANNEL, TEST_SERVER

    TEST_MODE = test_mode
    TECHNICAL_SUPPORT_SERVER = int(os.getenv("TECHNICAL_SUPPORT_SERVER" if not TEST_MODE else "TECHNICAL_SUPPORT_SERVER_TEST"))
    AI_SIGNAL_CHANNEL = int(os.getenv("AI_SIGNAL_CHANNEL" if not TEST_MODE else "AI_SIGNAL_CHANNEL_TEST"))
    TEST_SERVER = int(os.getenv("TEST_SERVER"))