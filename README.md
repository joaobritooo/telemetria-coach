# Telemetria Coach

Painel de telemetria em tempo real para **Assetto Corsa Competizione** e **Assetto Corsa**, feito para o monitor secundário. Traz coach de pilotagem por voz, dicas de setup, rivais ao vivo, estratégia de pit e combustível, chuva e desgaste de pneus, spotter, mapa da pista e a evolução do piloto entre sessões.

## ⬇️ Download

**[Baixar TelemetriaCoach.exe](https://github.com/joaobritooo/telemetria-coach/releases/latest/download/TelemetriaCoach.exe)** (Windows 10/11, não precisa instalar Python)

1. Baixe o `.exe` e coloque numa pasta só dele, porque o app cria uma pasta `dados` ao lado.
2. Dê dois cliques. Se o Windows SmartScreen avisar, clique em *Mais informações → Executar assim mesmo*. O app não é assinado digitalmente.
3. Abra o ACC ou o AC e entre na pista. O painel abre sozinho no monitor secundário.

Para testar sem o jogo, crie um atalho do `.exe` e acrescente `--demo` no campo *Destino*.

## Rodar pelo código-fonte

```
pip install -r requirements.txt
python main.py          # ou: python main.py --demo
```

O manual completo (o que o app faz, as opções e os ajustes) está em [LEIA-ME.md](LEIA-ME.md).

## Gerar o .exe

```
pip install pyinstaller pywebview
python -m PyInstaller --noconfirm --onefile --windowed --name TelemetriaCoach --icon icone.ico --add-data "web;web" --collect-all webview main.py
```
