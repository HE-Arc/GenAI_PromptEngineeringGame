# Jeu GenAI — prompt engineering en classe

Spécification complète : [wiki Cahier-des-charges](https://github.com/HE-Arc/GenAI_PromptEngineeringGame/wiki/Cahier-des-charges).

## Lancer avec Docker

```bash
cp server/.env.example server/.env   # remplir LLM_API_KEY (passerelle OpenCode Go) et ADMIN_PASSWORD
docker compose up --build
```

IP locale : celle de la machine sur le Wi-Fi de la classe (même Wi-Fi que les téléphones).
- macOS (Wi-Fi) : `ipconfig getifaddr en0` (câble : `en1`)
- Linux : `hostname -I | grep -o -m1 '172\.[0-9.]*' || hostname -I | grep -o -m1 '192\.[0-9.]*'`
- Windows : `ipconfig`, section Wi-Fi (adresse IPv4)

- Joueurs : `http://<IP>:5173` (code de partie + pseudo)
- Admin : `http://<IP>:5173/admin` (mot de passe)
