# Jeu GenAI — prompt engineering en classe

Spécification complète : [wiki Cahier-des-charges](https://github.com/HE-Arc/GenAI_PromptEngineeringGame/wiki/Cahier-des-charges).

## Lancer avec Docker (host classe, sans build)

```bash
cp secrets.env.example secrets.env   # remplir LLM_API_KEY (passerelle OpenCode Go) et ADMIN_PASSWORD
docker compose pull && docker compose up -d
docker compose logs -f               # logs host (rotation 5 x 10M)
```

IP locale : celle de la machine sur le Wi-Fi de la classe (même Wi-Fi que les téléphones).
- macOS (Wi-Fi) : `ipconfig getifaddr en0` (câble : `en1`)
- Linux : `hostname -I | grep -o -m1 '172\.[0-9.]*' || hostname -I | grep -o -m1 '192\.[0-9.]*'`
- Windows : `ipconfig`, section Wi-Fi (adresse IPv4)

- Joueurs : `http://<IP>:3001` (code de partie + pseudo)
- Admin : `http://<IP>:3001/admin` (mot de passe)

## Déployer sur k8s (image unique `codexis/genai-prompt-game:latest`)

```bash
cp secrets.env.example secrets.env   # remplir, jamais commité
kubectl create secret generic genai-secrets --from-env-file=secrets.env
kubectl apply -k k8s/
kubectl port-forward svc/genai-game 3001:80   # check -> http://localhost:3001/healthz
```

CI (`docker-publish.yml`) build+push l'image sur chaque push main/master, tag `v*`, ou manuel. Secrets GH requis : `DOCKERHUB_USERNAME`, `DOCKERHUB_TOKEN`.
