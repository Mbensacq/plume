# Runtime hook PyInstaller — variante « CPU / allégée » de Plume.
# Exécuté AVANT plume.py (donc avant la lecture des constantes du module).
# Impose le CPU (ce build n'embarque pas CUDA) et un modèle plus léger par défaut.
# setdefault => l'utilisateur peut toujours surcharger via les variables d'env.
import os

os.environ.setdefault("PLUME_DEVICE", "cpu")   # pas de GPU dans ce build
os.environ.setdefault("PLUME_MODEL", "medium")  # plus rapide/léger que large-v3
