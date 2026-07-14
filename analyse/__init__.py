"""
Analyses pandas du Moniteur Claude Code — package OPTIONNEL.

Seule exception assumee au principe zero-dependance du projet : ce package
importe pandas, mais il n'est charge que paresseusement par la route
/api/analyse de server.py (`import analyse`). Sans pandas, tout le reste du
Moniteur fonctionne a l'identique. Le calcul vit dans calculs.py ; ce
__init__ ne fait que re-exporter l'API publique.
"""

from analyse.calculs import JOURS_FR, compute

__all__ = ["JOURS_FR", "compute"]
