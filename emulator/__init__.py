"""The pigeon machine: CPU, RAM, IO bus, and devices.

Deliberately exports nothing. The assembler imports
``emulator.instruction_set``; re-exporting Machine here would drag
FastAPI and uvicorn into every assembler run.
"""
