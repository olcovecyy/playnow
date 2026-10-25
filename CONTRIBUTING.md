# Přispívání

Menší opravy, dokumentace a srozumitelnější ovládání jsou vítané. V issue popiš očekávané chování a kroky k reprodukci. Přikládej jen umělá testovací data; žádná rodinná videa, hesla nebo platné sdílecí odkazy.

## Lokální vývoj

Použij Python 3.12 a samostatné prostředí:

```sh
python3 -m venv .venv
.venv/bin/python -m pip install -r app/requirements.txt
.venv/bin/python -m unittest -q test_backend test_setup
```

Testy vytvářejí vlastní dočasná úložiště a testovací přístupové údaje. Ke své soukromé databázi je nepřipojuj. Na Windows použij Linuxové prostředí nebo Docker; aplikace používá POSIX zámky a hardlinky.

Pro úpravy klienta zkontroluj syntaxi také pomocí Node.js:

```sh
node --check app/static/app.js
```

Docker sestavení:

```sh
docker build -t playnow:dev .
```

## Co musí zůstat funkční

- Oddělená role diváka a správce, chráněná média, CSRF a odvolatelné odkazy na jediné video.
- Upload po částech, bezpečné navázání a zachování originálu.
- Přejmenování bez přepsání cizího souboru, oprava přerušené operace a stejné ID videa.
- Oddělená konfigurace, metadata a média; chybějící mediální disk znamená chybu, nikoli náhradní ukládání.
- Responzivní české rozhraní a pravdivé stavy přípravy videa.

K pull requestu přidej stručný popis problému, výsledné změny a provedené kontroly. Změny rozhraní dolož snímkem s umělými daty. U změn úložiště ověř i přerušení operace a obnovu; u změn přístupů také nepovolené scénáře. Nezaváděj obecný refaktor kvůli malé opravě.
