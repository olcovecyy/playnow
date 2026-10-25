# Pokyny pro AI agenty

PlayNow je soukromý přehrávač a katalog vlastních videí. Udržuj české rozhraní a oddělené role diváka a správce. Nezaměňuj titul videa s fyzickým názvem souboru.

## Nejdřív zjisti skutečný stav

- Přečti `README.md`, `docs/deployment.md` a nápovědu instalačního skriptu. Použij existující konfiguraci; opakované nasazení nesmí vygenerovat nová hesla ani změnit úložiště.
- Na cílovém hostu zkontroluj volné místo, připojený disk, UID/GID, obsazené porty a běžící služby. Před změnou zaznamenej identitu a čas spuštění nesouvisejících kontejnerů.
- Doménu, DNS, SSH přístup a případný VPS ověř z uživatelem poskytnutých údajů. Pokud oprávnění nebo zásadní údaj chybí, požádej o něj. Nevymýšlej účty, domény ani přístupové údaje.

## Provozní hranice

- Nasazuj pouze samostatný Compose projekt PlayNow. Nerestartuj celý host, Docker ani cizí kontejnery; nezastavuj existující weby kvůli zabrání jejich portů.
- Před aktualizací uchovej ověřitelnou konzistentní zálohu databáze, nastavení a médií. `Originaly/<id>/.source` a pojmenovaný originál jsou hardlinky: zálohovací nástroj je musí zachovat. Nikdy nepoužívej `docker compose down -v` ani plošný prune.
- Nevkládej hesla, privátní klíče, `.env`, databáze ani soukromé sdílecí odkazy do chatu, výpisů, commitu nebo issue. S tajemstvími pracuj v lokálních souborech s omezenými právy.
- Úložiště nesmí při chybě připojení disku tiše přejít jinam. Zachovej kontrolu značky disku, limity uploadu, rezervu místa a zámky souborů.
- Nikdy neobcházej ověření SSH hostitelského klíče. Tunel vystavuje port pouze na loopbacku VPS; veřejný vstup obsluhuje HTTPS proxy.

## Při úpravách

- Zachovej kontrolu přihlášení i na přímých adresách videí, CSRF, omezení pokusů, sdílení omezené na jedno video a jeho odvolání.
- Neměň ID videa při přejmenování. Nepřepisuj kolidující soubor; zachovej obnovitelné přejmenování a původní obsah.
- Upload zpracovávej po částech. Otisk pro navázání používá pouze malé vzorky; není důkazem úplné integrity celého videa.
- Nedělej živý převod 4K videa. Samostatný worker připravuje menší přehrávací kopii; originál zachovává.
- Spusť kontroly uvedené v `CONTRIBUTING.md` přiměřené změně. U změn rozhraní prohlédni skutečný výsledek na telefonu i počítači.

## Před předáním

Ověř zdraví aplikace, nepřihlášený přístup, diváka, správce, navázání uploadu, přehrávání s HTTP Range a odvolání sdílení. U veřejného nasazení navíc ověř DNS, platné HTTPS a přístup z jiné sítě. Porovnej původní stav nesouvisejících služeb.

Výsledek popiš přesně: zdrojové změny, lokální test, nasazená aplikace a skutečně ověřený veřejný přístup jsou různé stavy. Netvrď fyzické ověření na iPhonu ani plynulé 4K/120 fps, pokud takový test neproběhl.
