@echo off
cd /d "%~dp0"
title Lancement du Moniteur Claude Code

echo ================================================================
echo   DEMARRAGE DU MONITEUR CLAUDE CODE
echo ================================================================
echo.
echo   1/2 - Demarrage du moniteur dans une nouvelle fenetre...

rem Lance le serveur dans sa propre fenetre (a laisser ouverte)
start "Moniteur Claude Code" "%~dp01-demarrer-moniteur.bat"

echo   2/2 - Ouverture du tableau de bord dans le navigateur...

rem Laisse quelques secondes au serveur pour demarrer
timeout /t 4 /nobreak >nul

start "" "http://localhost:4318/"

echo.
echo ================================================================
echo   OK - Tout est lance.
echo.
echo   - Le moniteur tourne dans la fenetre "Moniteur Claude Code"
echo     (ne la fermez pas tant que vous voulez collecter les donnees).
echo   - Le tableau de bord vient de s'ouvrir dans votre navigateur.
echo.
echo   Il ne reste plus qu'a ouvrir un NOUVEAU terminal et lancer :
echo       claude
echo ================================================================
echo.
echo   (Cette fenetre-ci peut etre fermee.)
timeout /t 6 /nobreak >nul
exit /b
