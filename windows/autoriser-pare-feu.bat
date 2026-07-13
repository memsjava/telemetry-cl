@echo off
rem A lancer UNIQUEMENT sur la machine qui heberge le moniteur,
rem et SEULEMENT si d'autres machines doivent lui envoyer des donnees.
net session >nul 2>nul
if not %errorlevel%==0 (
  echo.
  echo   Ce fichier doit etre lance en tant qu'ADMINISTRATEUR.
  echo   Clic droit sur le fichier  ^>  "Executer en tant qu'administrateur".
  echo.
  pause
  exit /b
)
netsh advfirewall firewall add rule name="Moniteur Claude Code" dir=in action=allow protocol=TCP localport=4318 >nul
echo.
echo   OK - Le port 4318 est autorise en entree.
echo.
pause
