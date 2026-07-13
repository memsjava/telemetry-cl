@echo off
cd /d "%~dp0.."
title Moniteur Claude Code
where python >nul 2>nul
if %errorlevel%==0 (set PY=python) else (set PY=py)
echo ================================================================
echo   MONITEUR CLAUDE CODE
echo ================================================================
echo.
echo   Tableau de bord : http://localhost:4318/
echo   Laissez cette fenetre OUVERTE (fermez-la pour arreter).
echo.
echo ================================================================
echo.
%PY% server.py
echo.
echo Le moniteur est arrete.
pause
