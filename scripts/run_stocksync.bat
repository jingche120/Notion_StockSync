@echo off
rem Notion_StockSync launcher (called by Task Scheduler)
rem This file lives in scripts\ ; project root is one level up.
cd /d "%~dp0.."
".venv\Scripts\python.exe" main.py
