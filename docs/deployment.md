# Nasazení PlayNow

Níže jsou postupy pro vlastní server. Domény `videos.example.com` a `vps.example.net` jsou zástupné; nahraď je svými. Dokumentace sama DNS, certifikát ani tunel nevytváří.

## 1. Připrav samostatnou instalaci

Ověř volné místo, připojený datový disk, oprávnění a obsazené porty. Zachovej současné služby. PlayNow potřebuje místo na originály, menší přehrávací kopie i rozpracované uploady; navíc standardně nechává 10 GiB rezervu. Médium musí podporovat hardlinky v rámci stejného souborového systému.

Pokud na hostu již existuje Compose projekt `playnow`, zvol jiné jméno, například `docker compose -p playnow-rodina …`. Stejné `-p` pak přidávej ke všem příkazům pro spuštění, kontrolu, zálohu i aktualizaci. Příklady dále předpokládají výchozí jméno a port `8080`; vlastní port změň také v proxy a tunelu.

Instalátor používá pouze standardní knihovnu Pythonu; aplikace běží v Dockeru:

```sh
git clone https://github.com/olcovecyy/playnow.git
cd playnow
python3 scripts/setup.py --help
python3 scripts/setup.py --media-dir /mnt/videos/PlayNow --public-url https://videos.example.com
```

Adresář `/mnt/videos` musí existovat na správném disku a být zapisovatelný. Jeho podadresář `PlayNow` musí být nový. Stejně tak musí být nový adresář metadat, výchozí `var/data`. Instalátor odmítne existující konfiguraci i existující cílové adresáře; neslouží k migraci ani k opakovanému nastavení.

Důležité volby:

| Volba | Význam |
| --- | --- |
| `--public-url` | Přesná vnější adresa bez další cesty, např. `https://videos.example.com`. |
| `--port` | Hostitelský loopback port aplikace; výchozí `8080`. |
| `--data-dir` | Nový soukromý adresář pro SQLite, náhledy a zámky. |
| `--media-dir` | Nový adresář na datovém disku pro všechna videa. |
| `--uid`, `--gid` | Vlastník nových souborů a procesů v kontejnerech; běžně aktuální neprivilegovaný uživatel. |
| `--viewer-password-file`, `--admin-password-file` | Alternativa pro automatizaci: vlastní lokální soubory s hesly a právy `0600`. |

Interaktivně zadej dvě odlišná hesla o alespoň 12 znacích. Při práci jako root zadej výslovně vhodné neprivilegované UID/GID. Instalátor nemění vlastníka již existujících adresářů.

Vzniknou `.env` s cestami a parametry Compose a `runtime.env` s hashi hesel, tajným klíčem a nastavením aplikace. Oba soubory udržuj mimo Git a veřejné výpisy. `runtime.env` používá uvozování pro Compose; nepředávej jej jako `docker run --env-file`.

## 2. Vyber veřejný vstup

### A. Doména přímo na tento server

A/AAAA záznam domény musí vést na server a porty 80/443 musí být dostupné z internetu. Nesprávný AAAA záznam může přístup pokazit. Pro přiložený Caddy spusť:

```sh
docker compose -f compose.yaml -f deploy/compose.https.yaml config --quiet
docker compose -f compose.yaml -f deploy/compose.https.yaml up -d --build
```

Caddy získává a obnovuje certifikát pro nakonfigurovanou doménu; jeho certifikační data zůstávají v oddělených volumes. Předpoklady popisuje [oficiální návod HTTPS](https://caddyserver.com/docs/automatic-https).

Pokud porty už používá existující proxy, zachovej ji a spusť **jen základní** Compose:

```sh
docker compose config --quiet
docker compose up -d --build
```

Do existující proxy přidej samostatnou doménu směřující na `127.0.0.1:8080`. U Caddy běžícího přímo na hostu může mít blok podobu:

```caddyfile
videos.example.com {
    request_body {
        max_size 9MiB
    }
    reverse_proxy 127.0.0.1:8080 {
        header_up X-Forwarded-For {remote_host}
        header_up X-Forwarded-Proto https
    }
}
```

Zachovej veřejnou hlavičku `Host`, HTTPS schéma a bezpečně nahrazované forwarding hlavičky. Limit požadavku musí pojmout 8MiB upload část; není potřeba limit 100 GiB na jediný požadavek. V kontejneru `127.0.0.1` označuje jeho vlastní síť, proto tento příklad patří hostitelské proxy. Přiložená kontejnerová varianta používá `web:8000`. Viz [Caddy reverse_proxy](https://caddyserver.com/docs/caddyfile/directives/reverse_proxy) a [síťování Compose](https://docs.docker.com/compose/how-tos/networking/).

### B. Domácí server za NAT + vlastní VPS

Tok spojení: **prohlížeč → HTTPS na VPS → loopback port VPS → SSH tunel → PlayNow doma**. Video zůstává doma; VPS přenáší provoz. Dostupnost i počet diváků závisí na domácím uploadu a připojení VPS.

Doma nastav `--public-url https://videos.example.com`, ale spusť pouze základní Compose. DNS domény vede na VPS. Caddy běžící přímo na VPS použije předchozí blok s upstreamem **`127.0.0.1:18080`**.

Pro tunel použij samostatný účet a klíč. Na VPS omez účet `playnow-tunnel` například následujícím SSH `Match` blokem:

```sshconfig
Match User playnow-tunnel
    PasswordAuthentication no
    KbdInteractiveAuthentication no
    AllowTcpForwarding remote
    AllowStreamLocalForwarding no
    PermitListen 127.0.0.1:18080
    GatewayPorts no
    PermitTTY no
    AllowAgentForwarding no
    X11Forwarding no
    ForceCommand /usr/bin/false
Match all
```

Způsob založení účtu a načtení tohoto bloku přizpůsob hostu. Než bezpečně načteš konfiguraci SSH, ověř ji pomocí `sshd -t` a ponech si původní správcovské spojení. Změna má platit pouze pro vyhrazený účet. Podrobnosti: [sshd_config](https://man.openbsd.org/sshd_config).

Jeho veřejný klíč v `authorized_keys` může mít tento prefix; skutečný veřejný klíč doplň za něj:

```text
restrict,port-forwarding,permitlisten="127.0.0.1:18080",command="/usr/bin/false" ssh-ed25519 VEŘEJNÝ_KLÍČ
```

Privátní klíč zůstává doma, ne v repozitáři. Ověř fingerprint hostitelského klíče VPS přes jeho konzoli nebo jinou důvěryhodnou cestu a vlož ověřený klíč do vyhrazeného `known_hosts`. Samotný síťový scan ověřením není. Omezení klíčů popisuje [OpenSSH](https://man.openbsd.org/sshd#AUTHORIZED_KEYS_FILE_FORMAT).

Na domácím hostu připrav chráněné soubory `/etc/playnow-tunnel/key`, `/etc/playnow-tunnel/known_hosts` a tento `/etc/playnow-tunnel/ssh_config`, čitelné pouze účtem tunelu:

```sshconfig
Host playnow-relay
    HostName vps.example.net
    User playnow-tunnel
    IdentityFile /etc/playnow-tunnel/key
    IdentitiesOnly yes
    UserKnownHostsFile /etc/playnow-tunnel/known_hosts
    StrictHostKeyChecking yes
    BatchMode yes
    ExitOnForwardFailure yes
    ServerAliveInterval 30
    ServerAliveCountMax 3
    RemoteForward 127.0.0.1:18080 127.0.0.1:8080
```

Tunel spusť pod jeho lokálním neprivilegovaným účtem:

```sh
ssh -NT -F /etc/playnow-tunnel/ssh_config playnow-relay
```

Při výpadku musí správce procesu SSH znovu spustit; keepalive sám reconnect nedělá. Pro Linux se systemd lze použít vlastní službu:

```ini
[Unit]
Description=PlayNow reverse SSH tunnel
Wants=network-online.target
After=network-online.target

[Service]
User=playnow-tunnel
ExecStart=/usr/bin/ssh -NT -F /etc/playnow-tunnel/ssh_config playnow-relay
Restart=always
RestartSec=10

[Install]
WantedBy=multi-user.target
```

Oba účty `playnow-tunnel` musí být vytvořené na příslušných hostech. Soukromý klíč musí služba umět použít bez interaktivního dotazu. Port 18080 na VPS neotvírej veřejně. Syntaxi forwardu a dohled procesu popisují [ssh](https://man.openbsd.org/ssh), [ssh_config](https://man.openbsd.org/ssh_config) a [systemd.service](https://www.freedesktop.org/software/systemd/man/latest/systemd.service.html).

## 3. Ověř skutečné fungování

```sh
docker compose ps
curl --fail http://127.0.0.1:8080/healthz
```

U HTTPS varianty přidávej stejné `-f` přepínače jako při spuštění. Používej `config --quiet`; běžný výpis rozbalené konfigurace může obsahovat tajemství.

Z jiné sítě otevři veřejnou adresu. Zkontroluj platné HTTPS, přihlášení diváka a správce, odmítnutí přímého média bez přihlášení, malý upload včetně navázání a přehrávání s posunem. Vytvoř sdílecí odkaz, vyzkoušej jej bez původní relace a ověř přístup k celé knihovně i odmítnutí správy a uploadu. Následně odkaz zruš a ověř odmítnutí již přihlášeného příjemce. Samotné `/healthz` tyto vlastnosti nepotvrzuje.

Adresa v `PLAYNOW_PUBLIC_URL` musí odpovídat veřejnému originu. Pro HTTPS instalátor nastavuje secure cookie a jeden důvěryhodný proxy hop. Nepřidávej další hop naslepo. Aplikace kontroluje `Host`, takže lokální katalog nemusí pod jinou adresou fungovat, i když healthcheck projde.

### Přechod existující lokální instalace na HTTPS

Instalátor znovu nepouštěj. V existujícím `runtime.env` změň pouze tyto řádky pro jednu Caddy proxy; tajný klíč a hashe hesel zachovej:

```dotenv
PLAYNOW_PUBLIC_URL='https://videos.example.com'
PLAYNOW_COOKIE_SECURE='1'
PLAYNOW_TRUSTED_PROXY_COUNT='1'
```

V `.env` pro přiloženou HTTPS variantu nastav:

```dotenv
PLAYNOW_DOMAIN='videos.example.com'
PLAYNOW_HTTPS_PORT='443'
```

Pak znovu vytvoř pouze aplikační služby. Při přiloženém Caddy použij:

```sh
docker compose -f compose.yaml -f deploy/compose.https.yaml up -d web worker
```

Pokud Caddy nově přidáváš nebo měníš jeho doménu, stejným Compose příkazem spusť/aktualizuj také službu `caddy`. U vlastní proxy nebo SSH tunelu zůstaň u základního `docker compose up -d web worker` a uprav pouze příslušnou existující proxy. Použij zvolené jméno projektu `-p`, pokud se liší od výchozího. Přechod může přerušit aktuální upload; naplánuj jej po dokončení nebo pozastavení přenosu a nakonec znovu ověř veřejné HTTPS.

## Data, záloha a aktualizace

| Umístění | Obsah |
| --- | --- |
| Cesta `PLAYNOW_DATA_PATH` z `.env` | SQLite katalog, relace, sdílení, náhledy a zámky. |
| `PLAYNOW_MEDIA_PATH/Originaly/<id>/<název>` | Skutečný originál a jeho stabilní hardlink `.source`. |
| `PLAYNOW_MEDIA_PATH/Nahravani` | Rozpracované uploady. |
| `PLAYNOW_MEDIA_PATH/Prehravani` | Menší kopie a dočasné soubory workeru. |
| `runtime.env` | Hashe hesel, tajný klíč a aplikační nastavení; žádná obnovitelná hesla. |

Ověřovací soubor `.playnow-storage` patří k mediálnímu adresáři. Bez něj aplikace nevytvoří náhradní úložiště jinde. Originál ručně nepřepisuj; přejmenování dělej přes Správce. Médium po nahrání automaticky nemonitoruje přidané externí soubory.

Pro konzistentní jednoduchou zálohu přeruš nové uploady, zastav **jen PlayNow web a worker**, pak archivuj konfiguraci a oba datové adresáře. Pro výchozí cesty:

```sh
umask 077
mkdir -p backups
playnow_backup="backups/playnow-$(date -u +%Y%m%dT%H%M%SZ).tar"
docker compose stop web worker
tar -cpf "$playnow_backup" .env runtime.env var/data var/media
tar -tf "$playnow_backup" >/dev/null
docker compose up -d web worker
```

U vlastních cest nahraď `var/data` a `var/media` skutečnými adresáři z `.env`. Archiv musí obsahovat skryté soubory a zachovat hardlinky; nepoužívej volbu rozbalující hardlinky na nezávislé kopie. Uchovej i Caddy volumes, pokud jej provozuješ. Zálohu přenes mimo tento disk a ověř obnovou do izolovaného umístění. Při živé databázi nestačí kopírovat jen `catalog.sqlite3`; jinou možností je [konzistentní SQLite backup](https://www.sqlite.org/backup.html).

Před aktualizací uchovej také identitu dosavadního obrazu/commitu. Po záloze:

```sh
git pull --ff-only
docker compose build
docker compose up -d web worker
```

Znovu proveď kontroly přístupů a přehrávání. Kontejnery lze nahradit při zachování dat; své bind mounty ani Caddy volumes nemaž. `docker compose down -v` ani plošný prune sem nepatří. Princip oddělených trvalých dat vysvětluje [Docker storage](https://docs.docker.com/engine/storage/volumes/).

Při obnově nepřepisuj běžící instalaci. Nejdřív zastav vlastní procesy a obnov konfiguraci, katalog i média jako jeden celek do ověřených cest se správným UID/GID. Databázi a `.source` hardlinky zkontroluj před spuštěním. Po změně schématu nevracej pouze starý obraz bez odpovídající ověřené zálohy.
