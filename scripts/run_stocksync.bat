@echo off
REM =====================================================
REM  Notion_StockSync 啟動腳本（Windows 工作排程器用）
REM  排程器每週一~五 08:45 呼叫本檔即可
REM =====================================================

REM 切到專案根目錄（本檔位於 scripts\，上一層即根目錄；
REM .env / df_base_data.json / logs\ / 富邦憑證 都是相對根目錄的路徑）
cd /d "%~dp0.."

REM 直接用 venv 的 python 執行，不需 activate
".venv\Scripts\python.exe" main.py
