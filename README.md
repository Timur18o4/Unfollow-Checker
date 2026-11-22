# Unfollow Checker

Ein kleines Web-Setup für Raspberry Pi OS. Der Unfollow Checker gleicht regelmäßig die Twitch-Followerliste eines Kanals ab und meldet entfernte Follower im Twitch-Chat.

## Was das Programm macht

- Liest die Followerliste über die offizielle Twitch-API aus.
- Prüft standardmäßig alle 10 Minuten.
- Speichert beim ersten erfolgreichen Abruf einen Ausgangsstand und sendet dabei keine Chatnachricht.
- Meldet einen entfernten Follower als Unfollow, wenn dessen Twitch-Konto noch über die API erreichbar ist.
- Meldet ein entferntes Konto als nicht mehr erreichbar. Das kann auf ein deaktiviertes, gelöschtes oder gesperrtes Konto hinweisen; die API unterscheidet diese Gründe nicht zuverlässig.
- Sendet Nachrichten entweder über denselben Twitch-Account oder über ein zweites Chatkonto.
- Erneuert OAuth-Zugriffstokens automatisch. Tokens und Datenbank bleiben auf dem Raspberry Pi.

## Voraussetzungen

- Raspberry Pi mit Raspberry Pi OS oder Debian und systemd
- Internetzugriff vom Pi
- Ein Twitch-Konto für den überwachten Kanal
- Optional ein zweites Twitch-Konto, das in dessen Chat schreiben soll

Der öffentliche Twitch Client ID dieser App ist im Programm hinterlegt. Eine Client ID ist kein Zugriffstoken. Der Installer fragt weder Client Secret noch Tokens ab.

## Installation

Auf dem Raspberry Pi anmelden (lokal oder per SSH) und diese Befehle ausführen:

```bash
sudo apt update
sudo apt install -y git python3
git clone https://github.com/Timur18o4/Unfollow-Checker.git
cd Unfollow-Checker
sudo ./install.sh
```

Der Installer richtet den systemd-Dienst ein, aktiviert den Autostart und zeigt eine einmalige Einrichtungs-PIN an. Die PIN für die Weboberfläche notieren.

Öffne dann im Browser entweder auf dem Pi `http://localhost:8765` oder auf einem Gerät im selben Heimnetz die vom Installer angezeigte Adresse, zum Beispiel `http://192.168.178.36:8765`. Gib die PIN ein.

## Einrichtung im Browser

1. Twitch-Kanalnamen eingeben, dessen Followerliste überwacht werden soll.
2. Entscheiden, ob derselbe Account prüfen und im Chat schreiben soll oder ein zweiter Account für Chatnachrichten genutzt wird.
3. Für jedes angezeigte Konto **Freigeben** anklicken. Twitch zeigt einen einmaligen Gerätecode und einen Link an.
4. Den Link öffnen, beim passenden Twitch-Konto anmelden und den Code bestätigen. Es muss kein Token kopiert oder in die Weboberfläche eingefügt werden.
5. Der Checker liest die Followerliste und speichert sie als Ausgangsstand. Der systemd-Dienst läuft danach im Hintergrund.

Für den Kanalzugriff wird `moderator:read:followers` angefordert. Zum Schreiben in den Chat wird `user:write:chat` angefordert. Bei einem einzelnen Account werden beide Rechte für diesen Account angefragt; bei zwei Accounts erhält der Kanalaccount das Leserecht und das Chatkonto das Schreibrecht.

## Zugriff auf die Weboberfläche

Der Webserver lauscht standardmäßig im lokalen Netzwerk auf Port `8765`. Die Einrichtung kann daher auf dem Pi selbst oder von einem anderen Gerät im selben Netzwerk erfolgen.

Die PIN schützt die Oberfläche. Verwende sie nur in einem vertrauenswürdigen Heimnetz und richte keine Router-Portweiterleitung aus dem Internet auf Port 8765 ein. Die lokale Weboberfläche nutzt HTTP; ein Gerät mit Zugriff auf das lokale Netzwerk könnte Datenverkehr mitlesen. Die eigentlichen OAuth-Tokens werden vom Pi direkt mit Twitch ausgetauscht und nicht an den Browser zurückgegeben.

Bei Verlust der PIN eine neue erzeugen:

```bash
sudo /opt/unfollow-checker/reset-pin.sh
```

Der Befehl gibt die neue PIN im Terminal aus.

## Dienst verwalten

```bash
sudo systemctl status unfollow-checker.service
sudo systemctl restart unfollow-checker.service
sudo journalctl -u unfollow-checker.service -f
```

Der Dienst startet nach einem Neustart des Raspberry Pi automatisch. Die lokale Datenbank, OAuth-Tokens und PIN liegen im Home-Verzeichnis des Installationsbenutzers unter `.config/unfollow-checker/` und sind nur für diesen Benutzer lesbar.

## Hinweise

- Beim ersten Abruf gibt es keine rückwirkenden Follow- oder Unfollow-Meldungen.
- Unvollständige oder inkonsistente Twitch-Abrufe ändern den gespeicherten Stand nicht.
- Bei vielen gleichzeitigen Änderungen fasst der Checker die Ereignisse in einer Chatnachricht zusammen.
- Es wird keine Nachricht zum Testen in den Chat geschrieben.
- Zum Entfernen des Dienstes: `sudo systemctl disable --now unfollow-checker.service`, danach `/opt/unfollow-checker` und `~/.config/unfollow-checker` bei Bedarf manuell löschen.

## Technischer Hintergrund

Das Projekt benötigt keine Python-Pakete von Drittanbietern. Es nutzt Python-Standardbibliothek, SQLite, systemd und die Twitch Helix API. Siehe [Twitch Channel Followers API](https://dev.twitch.tv/docs/api/reference/channels), [Send Chat Message API](https://dev.twitch.tv/docs/api/reference/chat) und [OAuth Device Code Flow](https://dev.twitch.tv/docs/authentication/getting-tokens-oauth/).
