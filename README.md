# Party Sync : JBL PartyBox 310 + PartyLight Stick sur les mêmes couleurs

Outil **non officiel**, sans lien avec JBL ou Harman. À utiliser à tes risques.

## En bref

- **Pourquoi ils ne se synchronisent pas tout seuls.** Le PartyLight Stick se synchronise automatiquement avec les PartyBox qui ont **Auracast** (Club 120, Stage 320, 520…). La 310 (Bluetooth 5.1) ne l'a pas.
- **Ce qu'on a trouvé.** La 310 et le stick parlent **le même protocole Bluetooth Low Energy**, celui de l'appli JBL PartyBox, avec les mêmes commandes de couleur et de motif.
- **Donc** un programme peut se connecter aux deux et leur envoyer la même commande au même moment. C'est ce que font les deux versions ci-dessous.

| Fichier | Où ça tourne | Pour quoi |
|---|---|---|
| `jbl_sync_web.html` | iPhone (appli **Bluefy**), Android (**Chrome**), PC (**Chrome / Edge**) | Rien à installer : couleurs, motifs, mode fête, rythme au micro |
| `jbl_sync.py` | PC Windows / Mac / Linux ou Raspberry Pi, avec Python | Ligne de commande, télécommande web pour le téléphone, rythme au micro du PC, outils de rétro-ingénierie |

**Avant tout, dans les deux cas :** ferme complètement l'appli JBL PartyBox. Chaque appareil n'accepte **qu'une seule connexion de contrôle** à la fois. La musique peut continuer depuis ton téléphone : l'audio Bluetooth est une liaison séparée.

---

## 1. Version navigateur : `jbl_sync_web.html`

1. Ouvre la page dans un navigateur qui gère le **Bluetooth web** :
   - **iPhone** : l'appli gratuite *Bluefy – Web BLE Browser*. Safari ne gère pas le Bluetooth web, et Chrome sur iPhone en général non plus.
   - **Android** : Chrome (ou Samsung Internet).
   - **PC** : Chrome ou Edge.
2. Touche **+ PartyLight** et choisis le stick, puis **+ Enceinte** et choisis la 310. Si un appareil n'apparaît pas dans la liste, coche « Montrer tous les appareils Bluetooth ».
3. Touche une couleur : les deux la prennent en même temps. Le stick passe en motif « Perso » pour l'afficher. Si la couleur bouge encore, essaie le motif « Freeze ».

Dans **Couleur perso**, les curseurs teinte, saturation et luminosité changent la couleur en direct, et **★ Enregistrer** garde la couleur dans « Mes couleurs » sur ce téléphone. Le **mode fête** change la couleur partout à intervalle régulier, ou sur les basses captées par le micro du téléphone. Coche « Avec mes couleurs » pour qu'il n'utilise que tes couleurs enregistrées. En bas de la page, le **journal** montre chaque trame envoyée (→) et reçue (←), et permet d'envoyer une trame brute.

### Où ouvrir la page

Le Bluetooth web ne marche que sur une page en **https://** ou sur **localhost**, ouverte à sa propre adresse. Il ne marche pas dans une page affichée à l'intérieur d'une autre : c'est le cas de l'aperçu dans Claude, où Bluefy reçoit la demande mais n'ouvre jamais sa liste d'appareils.

- **En ligne** : <https://damiennerri.github.io/party-sync/>, à ouvrir dans Bluefy sur iPhone, ou dans Chrome sur Android et sur PC.
- **Sur un PC, sans Internet** : dans le dossier du projet, lance `python -m http.server 8000`, puis ouvre `http://localhost:8000/jbl_sync_web.html` dans Chrome ou Edge.
- **Une autre adresse https, gratuite**, avec GitHub Pages :
  1. crée un dépôt public sur github.com ;
  2. envoies-y `jbl_sync_web.html` renommé en `index.html` (*Add file › Upload files*) ;
  3. dans *Settings › Pages*, choisis la branche `main` et enregistre ;
  4. ouvre `https://TON-NOM.github.io/NOM-DU-DEPOT/` dans Bluefy ou Chrome.

---

## 2. Version Python : `jbl_sync.py`

### Installation

- Python **3.10 ou plus**, et un PC ou un Raspberry Pi avec Bluetooth 4.0+ (BLE) à moins de 10 m environ.
- `pip install bleak`
- Optionnel, pour suivre le rythme au micro : `pip install sounddevice numpy`

### Premier test

```bash
python jbl_sync.py scan
python jbl_sync.py party --palette rouge,vert,bleu --interval 2 --count 3
```

`scan` doit afficher la 310 (`[ENCEINTE]`, produit `1f5d`) et le stick (`[LUMIÈRE]`). La deuxième commande doit faire passer les deux par rouge, vert puis bleu.

### Commandes

| Commande | Effet |
|---|---|
| `scan [--all]` | Liste les appareils JBL (avec `--all` : tous les appareils Bluetooth) |
| `color COULEUR [--motif M]` | Même couleur partout (le stick passe en motif Perso) |
| `pattern MOTIF [--couleur C]` | Même motif d'animation partout |
| `on [--motif M] [--couleur C]` / `off` | Allume / éteint les lumières |
| `stick --vitesse N --intensite N` | Vitesse et intensité du PartyLight (0 à 100) |
| `strobe on` / `strobe off` | Stroboscope de l'enceinte |
| `party` | Mode fête : `--interval S`, `--palette rouge,bleu,#00FF88`, `--melange`, `--motif M`, `--count N` |
| `party --beat [--tous-les N]` | Une nouvelle couleur sur les basses captées par le micro du PC (`--sensibilite`, `--mic`) |
| `mirror` | *(expérimental)* Recopie l'état lumière de la 310 sur le stick (voir les limites) |
| `web [--port 8765]` | Télécommande dans le navigateur du téléphone, sur le même Wi-Fi |
| `explore [ADRESSE]` | Rétro-ingénierie : liste du GATT, notifications en direct, envoi de trames brutes, journal |

Options valables pour toutes les commandes : `--box ADRESSE`, `--stick ADRESSE` (répétable pour plusieurs sticks), `--no-box`, `--no-stick`, `--scan-time S`, `-v` (affiche les trames).

- **Couleurs** : `rouge orange jaune vert turquoise cyan bleu indigo violet magenta rose blanc`, `#RRGGBB`, `R,G,B` ou `aleatoire`.
- **Motifs** : `neon loop bounce trim switch freeze perso off`. L'appli en connaît d'autres (`rock club flow ripple cross flash`) que la 310 refusera peut-être : le programme affiche alors « refusé par l'appareil ».

### Télécommande sur le téléphone

```bash
python jbl_sync.py web
```

Le programme affiche une adresse du type `http://192.168.1.20:8765`, à ouvrir sur le téléphone connecté au même Wi-Fi. N'importe quel navigateur convient ici, puisque c'est le PC qui fait le Bluetooth. Sous Windows, autorise Python quand le pare-feu le demande. Avec `--host 127.0.0.1`, la page n'est accessible que depuis le PC.

### Sur un Raspberry Pi, au démarrage

```ini
# /etc/systemd/system/party-sync.service
[Unit]
Description=Party Sync
After=bluetooth.target network-online.target

[Service]
ExecStart=/usr/bin/python3 /home/pi/party-sync/jbl_sync.py web
Restart=always
User=pi

[Install]
WantedBy=multi-user.target
```

Puis `sudo systemctl enable --now party-sync`.

---

## 3. Tester à la main avec nRF Connect (sans rien installer)

1. Ferme l'appli JBL. Dans nRF Connect, connecte-toi à « JBL PartyLight Stick ».
2. Ouvre le service `65786365-6c70-6f69-6e74-2e636f6d0000`. Active les notifications de `…0001`.
3. Écris sur `…0002` au format *BYTE ARRAY* :
   - `AA330400310108` : motif Perso
   - `AA33090036010132039600FF` : violet
4. La réponse `AA-00-02-33-00` sur `…0001` veut dire OK. `…33-FF` veut dire refusé.

Les mêmes trames marchent sur la 310. Attends 2-3 secondes après la connexion, ou envoie d'abord `AA1100`.

---

## 4. Comment ça marche : le protocole

| Élément | Valeur |
|---|---|
| Service GATT | `65786365-6c70-6f69-6e74-2e636f6d0000` (« excelpoint.com » en ASCII) |
| Écriture (PC → appareil) | `…0002` |
| Notifications (appareil → PC) | `…0001` |
| Trame | `AA <commande> <longueur> <données>`, sans somme de contrôle |
| Accusé | `AA 00 02 <commande> <statut>` : `00` = ok, `FF` = refusé |
| Battement | `AA EA 00` toutes les 5 s (comme l'appli) |
| Infos appareil | `AA 11 00` → `AA 12 …` |
| État lumière | `AA 31 00` → `AA 32 …` |
| Réglage lumière | `AA 33 <len> 00 <tag> <len> <valeur>…` (`00` = l'appareil connecté) |

Étiquettes de la page lumière :

| Tag | Rôle | Exemple |
|---|---|---|
| `31` | Motif : 0 éteint, 2 Neon, 8 Perso, 9 Loop, 10 Bounce, 11 Trim, 12 Switch, 13 Freeze | `AA 33 04 00 31 01 08` |
| `36` + `32` | Couleur perso + RVB | `AA 33 09 00 36 01 01 32 03 96 00 FF` (violet) |
| `99` | Interrupteur général (modèles récents) | `AA 33 04 00 99 01 00` |
| `33` | Stroboscope | `AA 33 04 00 33 01 00` |
| `45` / `46` | Intensité / vitesse, de 0 à 100 (PartyLight) | `AA 33 04 00 46 01 32` |
| `4A` | Liste des motifs disponibles (dans les réponses) | |

Particularités constatées :

- La 310 **ignore les premières commandes** pendant environ 2 s après la connexion. Le programme la « réveille » en envoyant `AA 11 00` jusqu'à ce qu'elle réponde.
- La 310 **ne renvoie pas l'état de ses lumières** : c'est ce qu'a constaté le projet qui l'a testée. Le mode `mirror` (310 → stick) ne peut donc probablement pas marcher. Le programme fait l'inverse : c'est lui qui choisit les couleurs pour les deux.
- Sur la 310, on éteint avec le motif `0`. Le programme envoie aussi l'interrupteur `99` pour les modèles récents : l'un des deux suffit.
- Les couleurs peuvent être un peu différentes d'un appareil à l'autre, car ce ne sont pas les mêmes LED.

## 5. Aller plus loin : capturer ce que fait l'appli JBL

- **Android** : dans *Options pour les développeurs*, active le *journal HCI Bluetooth*. Utilise l'appli JBL (change une couleur, un motif…), puis récupère le rapport de bug (`adb bugreport`). Ouvre `btsnoop_hci.log` dans Wireshark avec le filtre `btatt.opcode == 0x12 || btatt.opcode == 0x52` et cherche les trames qui commencent par `aa 33`.
- **iPhone** : il faut un Mac avec PacketLogger et le profil de journalisation Bluetooth d'Apple.
- **En direct** : `python jbl_sync.py explore` te connecte à un appareil, affiche tout ce qu'il envoie et te laisse taper des trames.

⚠️ N'envoie que des trames dont tu comprends le rôle : une commande inconnue peut dérégler un appareil. En cas de souci, réinitialise-le en suivant sa notice.

## 6. Dépannage

- **Aucun appareil trouvé** : vérifie que l'appli JBL est bien fermée (sur Android, *Forcer l'arrêt*), que les appareils sont allumés et à portée, et que le Bluetooth du PC est activé. Sous Linux : `sudo systemctl start bluetooth`.
- **« Connecté mais muet »** : l'appli JBL tient encore la connexion, ou l'enceinte est en veille. Allume-la.
- **Linux et audio** : si le PC envoie aussi la musique à la 310, BlueZ cache le contrôle BLE. Passe la musique par le téléphone, ou déconnecte l'audio du PC (`bluetoothctl disconnect ADRESSE`).
- **macOS** : les adresses sont des identifiants propres au Mac. Autorise le Terminal dans *Réglages › Confidentialité › Bluetooth*.
- **Windows** : pas besoin d'appairer. Si ça bloque, retire l'appareil des réglages Bluetooth de Windows et réessaie.

## 7. Tests

`python -m unittest test_jbl_sync -v` lance 30 tests sur des appareils simulés (trames, réveil de la 310, coupures, mode fête, télécommande web, commandes complètes). Aucun Bluetooth n'est nécessaire.

## Crédits

Le protocole a été décodé par d'autres. Ce projet s'appuie sur leurs travaux publics :

- [BaggaBruh/jbl-partybox-ha](https://github.com/BaggaBruh/jbl-partybox-ha) (MIT) : protocole tiré de l'appli JBL PartyBox décompilée, **testé sur une PartyBox 310**.
- [pauci/ha-jbl-partybox](https://github.com/pauci/ha-jbl-partybox) (MIT) : notes de protocole (Stage 320 : zones, stroboscope, motifs).
- [liands/homeassistant-windows](https://github.com/liands/homeassistant-windows) : intégration du PartyLight Stick (firmware 0.5.0) qui montre qu'il utilise le même protocole.
