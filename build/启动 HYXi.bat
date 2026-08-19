@echo off
chcp 65001 >nul
cd /d "%~dp0"
title HYXi

app\hyxi.exe

REM Keep this file ASCII-only. cmd.exe parses a .bat using the codepage in effect
REM while reading, so Chinese written here gets split mid-sequence after chcp and
REM the line breaks apart into bogus commands. All user-facing text comes from
REM hyxi.exe instead, which writes UTF-8 to the console chcp already switched.

echo.
pause
