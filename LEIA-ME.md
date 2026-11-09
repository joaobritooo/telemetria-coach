# Telemetria Coach — ACC e Assetto Corsa

Painel de telemetria em tempo real para o monitor secundário, com coach de pilotagem e dicas de setup.
Pelo código-fonte, só precisa do `pywebview` (`pip install -r requirements.txt`). O .exe não precisa de nada.

## Como usar

1. Dê dois cliques em **TelemetriaCoach.exe**. Não precisa de Python.
   - Para atalho com opções (ex.: demo), crie um atalho do .exe e adicione `--demo` no campo "Destino".
   - Os dados ficam na pasta `dados` ao lado do .exe; erros vão para `dados/log.txt`.
   - Para fechar: botão **✕ Fechar** no rodapé ou Alt+F4 com o painel em foco.
2. O painel abre numa janela própria em tela cheia no **monitor secundário** (sem Edge, não congela com o jogo em foco).
   Para regerar o .exe depois de mexer no código:
   `python -m PyInstaller --noconfirm --onefile --windowed --name TelemetriaCoach --add-data "web;web" --collect-all webview --distpath .. main.py`
3. Abra o ACC ou o AC e entre na pista. O app detecta o jogo sozinho.
4. Para testar sem o jogo: **demo.bat**.

Opções:

| Comando | O que faz |
|---|---|
| `--sim acc` / `--sim ac` | força o simulador (se a autodetecção falhar) |
| `--monitor 1` | escolhe o monitor (1 = principal) |
| `--windowed` | abre em janela, sem tela cheia (F11 alterna) |
| `--no-window` | não abre o painel; acesse `http://localhost:8765` (dá até no celular/tablet, trocando `127.0.0.1` por `0.0.0.0` em `main.py`) |
| `--demo --demo-speed 3` | demo com o tempo 3x mais rápido |

## O que ele faz

- **Delta ao vivo** contra a sua melhor volta válida. A melhor volta fica salva em `dados/referencias/` por carro + pista, então já vale na próxima sessão.
- **Mapeia as curvas sozinho** (T1, T2…) a partir da volta de referência.
- **Próxima curva**: barra de contagem até o ponto de frenagem da referência, velocidade mínima e marcha.
- **Feedback na saída de cada curva**: tempo ganho/perdido e o motivo (freou cedo/tarde, mínima baixa, saída lenta, travou, patinou, coasting, marcha diferente).
- **Alertas instantâneos**: travamento de roda (por roda), patinagem, coasting, freio+acelerador juntos, volta invalidada.
- **Onde focar**: curvas em que você mais perde tempo em média.
- **Equilíbrio estimado** (sub/sobresterço) por fase (entrada/meio/saída) e por curvas lentas/rápidas, a partir dos ângulos de deriva dos pneus.
- **Dicas de setup**: pressão (quanto mexer a frio), camber (AC, pelas temperaturas interna/meio/externa), temperatura dos pneus e freios (dutos), brake bias, barras, diferencial, asa, TC/ABS, consumo e combustível.
- **Voz** (botão no rodapé): o coach fala os alertas em português.
- Histórico de cada sessão salvo em `dados/sessoes/`.

## Coaching por fases da curva

O coach divide cada passagem em frenagem → soltura do freio → rolagem → acelerador, com base em
material de coaching real (botão **📚 Conceitos e fontes** no painel):

- Rolar um pouco entre soltar o freio e acelerar é **normal** (o carro precisa girar). Só vira alerta
  quando passa da folga do tipo de curva **e** é maior que na sua melhor volta.
- Acelerar antes de poder abrir o volante e ter que aliviar = alerta (erro mais comum segundo os coaches).
- Tirar o pé antes de frear = alerta, exceto quando o app está em **modo economia** (lift & coast).

## Corrida (aba 🏁)

- **Rivais em tempo real (ACC):** classificação, gap para você, ritmo de cada um, quem está no box.
  Usa a Broadcasting API do ACC — o app lê `Documentos/Assetto Corsa Competizione/Config/broadcasting.json`
  sozinho (o seu já está com a porta 9000 ativa). O AC original não fornece dados dos rivais.
- **Duelo:** gap e ritmo do carro da frente e de trás, em quantas voltas alcança / é alcançado.
- **Combustível:** consumo médio, voltas até o fim, quanto falta/sobra, quanto colocar no pit, ou meta
  de consumo para economizar com lift & coast sem parar.
- **Pit:** perda estimada (25 s até você parar uma vez; depois usa a medida) e onde você volta na pista
  se parar agora. Avisos quando rivais próximos param (undercut/overcut).

## Ajustes

Os limites (pressão alvo, janelas de temperatura, sensibilidade de travamento etc.) ficam no topo de
`analysis.py`, no dicionário `CFG`. Os valores do ACC vêm para GT3/GT4 com pneus atuais
(26–27 psi quente no seco). No AC cada carro tem pressão ideal diferente — ajuste `ac_psi` para o carro que você usa.

## Se o painel travar

A cada 30 s o app grava em `dados/log.txt` uma linha `[desempenho]` com a taxa de leitura do jogo e o fps do painel.
O normal é ~60 Hz de leitura e ~20 fps de painel. Mande essas linhas se algo estiver lento.

## Observações

- O equilíbrio é uma **estimativa** pelos ângulos de deriva; use como tendência, não como verdade absoluta.
- No ACC, o jogo não fornece temperatura interna/meio/externa, então as dicas de camber só aparecem no AC.
- O brake bias mostrado no ACC é o valor bruto da telemetria (pode diferir alguns % do que aparece no jogo, varia por carro).
- AC EVO: não testado. Se ele expuser a mesma shared memory do AC, `--sim ac` deve funcionar.
