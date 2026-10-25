# Nasazení s AI agentem

Agent potřebuje přístup ke zvolenému serveru, místo pro média a údaj, zda použít přímou doménu, nebo vlastní VPS s reverzním SSH tunelem. Tento text můžeš vložit do nástroje, který umí pracovat s repozitářem a terminálem. Hesla ani privátní klíče do zadání nevkládej.

## Zadání ke zkopírování

```text
Nasaď PlayNow z https://github.com/olcovecyy/playnow na můj server jako samostatný Docker Compose projekt.

Cílový server nebo SSH alias: DOPLNÍM
Adresář pro instalaci: /srv/playnow
Disk/adresář pro videa: DOPLNÍM
Veřejná adresa: https://video.example.com
Způsob přístupu: přímá doména / reverzní SSH tunel přes můj VPS
Pokud potřebuji tunel, SSH alias VPS: DOPLNÍM

Nejdřív přečti AGENTS.md a dokumentaci nasazení. Ověř skutečné disky, volné místo, porty a běžící služby. Zachovej všechny ostatní aplikace a data, nerebootuj host a nerestartuj celý Docker.

Použij dodaný instalační skript a jeho aktuální nápovědu. Vytvoř oddělené heslo správce a diváka. Přístupové údaje ulož lokálně s omezenými právy; neposílej je do chatu, logů ani Gitu. Při opakovaném spuštění zachovej existující nastavení a hesla.

Média ukládej na vybraný disk s ověřovací značkou. Ponech bezpečnou rezervu místa. Samostatný worker může připravovat menší H.264 kopii do Full HD/60 fps; originál nesmí upravit ani smazat.

Doménu připoj přes HTTPS proxy. Pokud nejde přijímat spojení doma, vytvoř omezený reverzní SSH tunel na loopback mého VPS. Použij vyhrazený klíč, ověř a připni hostitelský klíč. Neobjednávej další služby. Ptej se jen na chybějící přístup, oprávnění nebo nezbytný údaj.

U existující instalace nejdřív vytvoř konzistentní zálohu konfigurace, databáze a médií se zachováním hardlinků. Žádné down -v, mazání volumes nebo plošné promazávání.

Nakonec ověř přihlášení obou rolí, odmítnutí nepřihlášeného přístupu k médiím, upload a navázání, přehrávání s posunem, sdílecí odkaz do celé knihovny a jeho zrušení. Odkaz nesmí umožnit správu ani upload; po zrušení musí přestat fungovat i dříve přihlášeným příjemcům. Použij malé vlastní testovací klipy. Ověř veřejné HTTPS zvenku a porovnej stav ostatních služeb.

Předej adresu katalogu a /admin, umístění konfiguračních souborů, postup zálohy a aktualizace a přesně rozliš provedené kontroly od dosud neověřených věcí. Neprohlašuj test na fyzickém iPhonu nebo plynulé 4K/120 fps bez skutečného testu.
```

## Co je potřeba dodat

Doména musí patřit tobě nebo musíš mít oprávnění ji spravovat. Pro tunel potřebuješ vlastní veřejně dostupný VPS a možnost přidat k němu HTTPS proxy. Podrobné příklady jsou v [návodu nasazení](deployment.md).

Agent může připravit konfiguraci, ale nemůže bez přístupu ověřit tvůj DNS účet, router, server ani telefon. Označení „hotovo“ má zahrnovat skutečnou kontrolu zvolené veřejné adresy.
