# Emploi du temps — Lycée

Application Streamlit de génération automatique de l'emploi du temps des spécialités (Terminale et Première).

---

## Déploiement sur Streamlit Community Cloud

### Prérequis

- Un compte GitHub avec ce repo (public ou privé)
- Un compte Streamlit Community Cloud — créer le compte sur [share.streamlit.io](https://share.streamlit.io) en se connectant avec GitHub

---

### Étapes

**1. Pousser le code sur GitHub**

Depuis le dossier du projet :

```bash
git add app.py solver.py data.py export.py requirements.txt .streamlit/config.toml
git commit -m "Initial app"
git push origin main
```

> Le fichier Excel de données (`TERMINALES-rentrée2026.xlsx`) est dans `.gitignore` — il ne sera pas poussé. L'utilisateur l'importera directement dans l'app.

---

**2. Créer l'app sur Streamlit Cloud**

1. Aller sur [share.streamlit.io](https://share.streamlit.io)
2. Cliquer **"New app"**
3. Remplir le formulaire :
   - **Repository** : `alexandre-cruel/emploi-du-temps`
   - **Branch** : `main`
   - **Main file path** : `app.py`
4. Cliquer **"Deploy!"**

Le déploiement prend 2–3 minutes (installation des dépendances depuis `requirements.txt`).

> **Note sur `ortools`** : la bibliothèque pèse ~150 MB. Si le déploiement dépasse 5 minutes ou échoue avec une erreur mémoire, voir la section [Dépannage](#dépannage) ci-dessous.

---

**3. Accéder à l'app**

Une fois déployée, l'URL est de la forme :

```
https://alexandre-cruel-emploi-du-temps-app-XXXXX.streamlit.app
```

Cette URL est partageable avec n'importe qui (navigateur, aucune installation requise).

---

**4. Mettre à jour l'app après modification du code**

Tout push sur la branche `main` redéploie l'app automatiquement (en quelques secondes si les dépendances n'ont pas changé).

```bash
git add .
git commit -m "description du changement"
git push origin main
```

---

## Utilisation de l'app

L'app fonctionne en 5 étapes séquentielles :

| Étape | Description |
|-------|-------------|
| **1 — Import** | Charger le fichier Excel des vœux élèves (onglet `Voeux élèves`) |
| **2 — Configuration** | Définir le nombre de groupes par spécialité, les créneaux disponibles, les contraintes prof |
| **3 — Résolution** | Générer automatiquement l'emploi du temps (bouton "Générer") |
| **4 — Ajustements** | Déplacer manuellement des élèves entre groupes avec détection de conflits |
| **5 — Export** | Télécharger le résultat en Excel (3 onglets : planning groupes, par élève, effectifs) |

### Format du fichier Excel attendu

L'onglet `Voeux élèves` doit contenir ces colonnes dans cet ordre :

| Colonne | Contenu |
|---------|---------|
| A | Nom |
| B | Prénom |
| C | Classe d'origine |
| D | Question 1 (situation — ignorée) |
| E | **Question 2 — Doublette de spécialités** (ex. `8) Maths - SPC`) |
| F | Question 4 — Option (ex. `Maths complémentaires`) |
| G | Question 5 — Option 2 |

---

## Règles encodées dans le solveur

### Créneaux (Terminale)

| Créneau | Jour | Horaire | Remarque |
|---------|------|---------|----------|
| Cr0 | Lundi | 13h55–15h50 | |
| Cr1 | Lundi | 15h50–17h50 | Réservé **Maths expertes** + DGEMC |
| Cr2 | Mardi | 8h10–10h | |
| Cr3 | Mardi | 10h15–12h05 | |
| Cr4 | Mercredi | 8h10–10h | Réservé **Maths complémentaires** |
| Cr5 | Jeudi | 8h10–10h | |
| Cr6 | Jeudi | 10h15–12h05 | |
| Cr7 | Vendredi | 8h10–10h | |
| Cr8 | Vendredi | 10h15–12h05 | |

### Contraintes automatiques

- **HLP-Philo** : cours uniquement le lundi (Cr0) et le jeudi (Cr5, Cr6) — prof absent les autres jours
- **Maths créneau commun** : tous les groupes Maths partagent le mercredi (Cr4) pour les évaluations communes
- **SPC et SVT** : 4 créneaux par semaine (au lieu de 3) — le créneau du mercredi (Cr4) leur est interdit pour libérer les élèves SPC-SVT sur Maths complémentaires
- **Maths expertes** : option retirée automatiquement si l'élève n'a pas Maths en spécialité

### Contraintes configurables (étape 2)

- Nombre de groupes par spécialité (max 38 élèves/groupe)
- Disponibilité des créneaux par spécialité (grille cochable)
- LCE : préférence pour éviter les débuts à 8h (Mardi/Jeudi/Vendredi)

---

## Développement local

```bash
# Installer les dépendances
pip install -r requirements.txt

# Lancer l'app
streamlit run app.py

# Lancer les tests
pytest tests/ -v
```

---

## Dépannage

**L'app ne démarre pas sur Streamlit Cloud (erreur mémoire ou timeout)**

Streamlit Community Cloud (plan gratuit) limite la RAM à ~1 GB. `ortools` est lourd à l'installation mais léger à l'exécution. Si le build échoue :
1. Vérifier que `requirements.txt` ne contient pas de version trop ancienne d'`ortools`
2. Forcer un redéploiement depuis le dashboard Streamlit : menu "⋮" → "Reboot app"

**"Aucun onglet 'Voeux élèves' trouvé"**

Le fichier Excel doit avoir un onglet nommé exactement `Voeux élèves` (avec l'accent et l'espace). Vérifier le nom de l'onglet dans Excel.

**Solution INFEASIBLE**

Le solveur ne trouve pas de solution. Causes fréquentes :
- Trop peu de créneaux activés pour une spécialité (SPC et SVT ont besoin de 4 créneaux)
- Contrainte HLP activée avec seulement 2 créneaux disponibles le lundi/jeudi
- Vérifier la grille de disponibilité à l'étape 2
