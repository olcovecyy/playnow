# PlayNow

**Soukromé kino pro rodinu a přátele. Na vlastním serveru.**

PlayNow je otevřený webový katalog a přehrávač vlastních videí. Nahraješ soubor, nastavíš přístup a pošleš odkaz. Divák nepotřebuje instalovat aplikaci.

## Co umí

- Katalog za heslem a samostatný **Správce** na `/admin` s odlišným heslem.
- Odkaz bez hesla na **jediné video**; jeho platnost může správce zrušit i již přihlášeným hostům.
- Upload po 8 MiB až do **100 GiB**, pozastavení a navázání opětovným výběrem stejného souboru. Na iPhonu nabízí výběr z Fotek i Souborů.
- Zachování originálu včetně 4K/120 fps a samostatnou menší H.264 kopii do **Full HD / 60 fps**. Přehrávač umožňuje obě verze přepínat.
- Úpravu titulku, popisu i skutečného názvu souboru při zachování jeho přípony a sdílecích odkazů.
- Docker Compose, oddělená metadata a mediální disk, volitelné HTTPS přes Caddy.

Skutečné přehrávání originálu závisí na kodeku, prohlížeči, zařízení a rychlosti připojení. Menší verzi připravuje omezený samostatný worker; nejde o živý převod 4K a příprava může trvat déle než samotné video. Na telefonu nech během velkého uploadu prohlížeč otevřený a displej odemčený. Uspání lze blokovat jen podle možností prohlížeče.

## Rychlé spuštění

Potřebuješ Docker s Compose pluginem, Python 3.9+ pro instalátor a disk podporující POSIX práva a hardlinky. Doporučený host je Linux. Návod počítá s novou instalací:

```sh
git clone https://github.com/olcovecyy/playnow.git
cd playnow
python3 scripts/setup.py
docker compose up -d --build
```

Instalátor se v terminálu skrytě zeptá na dvě různá hesla, každé alespoň 12 znaků. Uloží pouze jejich hashe a konfigurační tajemství do `runtime.env` s právy `0600`; hesla si ulož do správce hesel. Existující konfiguraci ani datové adresáře nepřepíše.

Na stejném počítači otevři [katalog](http://localhost:8080) nebo [Správce](http://localhost:8080/admin). Výchozí port je přístupný jen přes loopback. Pro telefon, rodinu a přátele připoj veřejné HTTPS podle [návodu nasazení](docs/deployment.md).

Chceš média rovnou na velkém disku? Při prvním spuštění zadej **nový vyhrazený adresář**:

```sh
python3 scripts/setup.py --media-dir /mnt/videos/PlayNow --public-url https://videos.example.com
```

Nepouštěj tento druhý příklad nad již vytvořenou instalací. Podrobnosti domény, vlastního proxy serveru, SSH tunelu, záloh a aktualizací jsou v [dokumentaci](docs/deployment.md).

## Nasazení pomocí AI

Dej agentovi tento repozitář a [hotové zadání](docs/ai-agent.md). Agent dostane postup pro samostatný Docker projekt, doménu nebo reverzní SSH tunel, ochranu existujících služeb a ověření výsledku. Hesla a privátní klíče mu neposílej v chatu.

## Vývoj a licence

Příspěvky: [CONTRIBUTING.md](CONTRIBUTING.md). Pokyny pro agenty: [AGENTS.md](AGENTS.md).

PlayNow je dostupné pod [MIT licencí](LICENSE). Soubory videí, konfigurace a provozní data do repozitáře nepatří.
