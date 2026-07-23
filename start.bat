@echo off
title AXIOM
where py >nul 2>nul
if %errorlevel%==0 (
    py axiom.py --server %*
) else (
    python axiom.py --server %*
)
pause
