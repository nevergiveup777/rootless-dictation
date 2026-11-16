// Rootless Dictation - Quick Access panel.
// Talks only to this plugin's own backend (main.py) through @decky/api's
// `callable`. Every row is a focusable control so the whole panel works with
// the D-pad; there are no inner scroll areas.
import {
  ButtonItem,
  DropdownItem,
  Field,
  PanelSection,
  PanelSectionRow,
  ToggleField,
  staticClasses,
} from "@decky/ui";
import { callable, definePlugin } from "@decky/api";
import { useEffect, useRef, useState } from "react";

type Last = {
  time: number;
  ok: boolean;
  text: string;
  sent: boolean;
  reason: string;
  seconds: number | null;
} | null;

type Status = {
  phase: string;
  enabled: boolean;
  settings: {
    lang: string;
    send: boolean;
    close_chat: boolean;
    mute: boolean;
    indicator: boolean;
    combo: string[];
    detailed_log: boolean;
  };
  capture: { state: string; message: string; held: string[]; ready: boolean; why: string; left: number };
  channel: string;
  error: string;
  last: Last;
  log: string[];
};

type Check = [label: string, ok: boolean, detail: string];

const getStatus = callable<[], Status>("status");
const setEnabled = callable<[enabled: boolean], string>("set_enabled");
const setSetting = callable<[name: string, value: string | boolean | string[]], string>("set_setting");
const selfTest = callable<[], Check[]>("self_test");
const captureButtons = callable<[], string>("capture_buttons");

const DEFAULT_COMBO = ["R1", "R2"];
const PHASE: Record<string, string> = {
  off: "Off",
  starting: "Starting…",
  choosing: "Choosing new buttons…",
  recording: "● Recording…",
  transcribing: "Turning speech into text…",
  typing: "Typing into chat…",
  error: "Stopped",
};

function readyText(s: Status, combo: string): string {
  const when = s.settings.indicator
    ? "talk when the red microphone appears"
    : s.settings.mute
      ? "talk when the game goes quiet"
      : "speak";
  return `Ready: hold ${combo}; ${when}; release a moment after your last word`;
}
const LANGS = [
  { data: "en", label: "English" },
  { data: "es", label: "Español" },
  { data: "auto", label: "Auto-detect (slower)" },
];

// Keep in step with KEYWORDS in engine/ptt_pipeline.py.
const WORDS_EN = "party, raid, guild, officer, say, yell, instance, reply, whisper + name";
const WORDS_ES = "grupo, banda, hermandad, oficial, di, grita, instancia, responde, susurra + nombre";
function channelWords(lang: string): string {
  const words = lang === "es" ? WORDS_ES : lang === "en" ? WORDS_EN : `${WORDS_EN} / ${WORDS_ES}`;
  return `Start with a channel word: ${words}. Without one, the open chat channel is used. ` +
    "Player names are often misheard, so a whisper may not reach them; reply needs no name.";
}

const small = { fontSize: "12px", lineHeight: "1.3", wordBreak: "break-word" as const };

function heldLabel(c: Status["capture"]): string {
  return c.held.length ? `Holding: ${c.held.join(" + ")}` : "Now hold the new buttons";
}

function heldText(c: Status["capture"]): string {
  if (!c.held.length) {
    return `Hold them together for about a second, then let go (${c.left} s left). Back buttons ` +
      "(L4, R4, L5, R5) work best: games rarely use them and they do not move around this menu.";
  }
  if (c.why) return `✖ ${c.why[0].toUpperCase()}${c.why.slice(1)}.`;
  return c.ready ? "✔ Let go now to save these buttons." : "Keep holding…";
}

function lastText(last: Last): string {
  if (!last) return "Nothing yet";
  if (!last.ok) return last.reason || "Not typed";
  const how = last.sent ? "sent" : "typed, NOT sent: send or clear it before talking again";
  const secs = last.seconds != null ? ` (${last.seconds} s)` : "";
  return `“${last.text}”: ${how}${secs}`;
}

function Content() {
  const [s, setS] = useState<Status | null>(null);
  const [note, setNote] = useState("");
  const [busy, setBusy] = useState(false);
  const [checks, setChecks] = useState<Check[] | null>(null);
  const fast = useRef(false); // poll faster while choosing buttons, to show them live

  useEffect(() => {
    let alive = true;
    let id: ReturnType<typeof setTimeout> | undefined;
    const tick = () =>
      getStatus()
        .then((r) => {
          if (!alive) return;
          fast.current = r.capture.state === "waiting";
          setS(r);
        })
        .catch((e) => alive && setNote(String(e)))
        .finally(() => {
          if (alive) id = setTimeout(tick, fast.current ? 200 : 1000);
        });
    tick();
    return () => {
      alive = false;
      if (id !== undefined) clearTimeout(id);
    };
  }, []);

  const act = async (p: Promise<string>) => {
    setBusy(true);
    try {
      const r = await p;
      setNote(r === "ok" ? "" : r);
      setS(await getStatus());
    } catch (e) {
      setNote(String(e));
    } finally {
      setBusy(false);
    }
  };

  const runSelfTest = async () => {
    setBusy(true);
    setChecks(null);
    try {
      setChecks(await selfTest());
    } catch (e) {
      setNote(String(e));
    } finally {
      setBusy(false);
    }
  };

  if (!s) {
    return (
      <PanelSection>
        <PanelSectionRow>
          <Field label="Loading…" focusable description={note || undefined} />
        </PanelSectionRow>
      </PanelSection>
    );
  }

  const combo = s.settings.combo.join("+");
  const isDefaultCombo = combo === DEFAULT_COMBO.join("+");
  const choosing = s.capture.state === "waiting";
  const status =
    s.phase === "error" && s.error
      ? `Stopped: ${s.error}`
      : s.phase === "listening"
        ? readyText(s, combo)
        : PHASE[s.phase] ?? s.phase;

  return (
    <>
      <PanelSection title="Push-to-talk">
        <PanelSectionRow>
          <ToggleField
            label={`Listen for ${combo}`}
            description="Test version: types into WoW Forever only."
            checked={s.enabled}
            disabled={busy || choosing}
            onChange={(v) => act(setEnabled(v))}
          />
        </PanelSectionRow>
        <PanelSectionRow>
          <Field label="Status" focusable highlightOnFocus description={<span style={small}>{status}</span>} />
        </PanelSectionRow>
        <PanelSectionRow>
          <Field label="Last message" focusable highlightOnFocus
            description={<span style={small}>{lastText(s.last)}</span>} />
        </PanelSectionRow>
        {note ? (
          <PanelSectionRow>
            <Field label="Note" focusable description={<span style={small}>{note}</span>} />
          </PanelSectionRow>
        ) : null}
      </PanelSection>

      <PanelSection title="Settings">
        <PanelSectionRow>
          <DropdownItem
            label="Language"
            menuLabel="Language"
            description={<span style={small}>{channelWords(s.settings.lang)}</span>}
            rgOptions={LANGS}
            selectedOption={s.settings.lang}
            disabled={busy}
            onChange={(o) => act(setSetting("lang", String(o.data)))}
          />
        </PanelSectionRow>
        <PanelSectionRow>
          <ToggleField
            label="Send automatically"
            description={
              s.settings.send
                ? "Your message is sent as soon as it is typed."
                : "Text is typed but not sent. Send or clear it before talking again."
            }
            checked={s.settings.send}
            disabled={busy}
            onChange={(v) => act(setSetting("send", v))}
          />
        </PanelSectionRow>
        {s.settings.send ? (
          <PanelSectionRow>
            <ToggleField
              label="Close chat after sending"
              description={
                s.settings.close_chat
                  ? "Presses Esc after sending, like B, so the chat box closes. Turn this off if your chat box already closes by itself: then Esc would open the game menu."
                  : "The chat box stays as the game leaves it after sending."
              }
              checked={s.settings.close_chat}
              disabled={busy}
              onChange={(v) => act(setSetting("close_chat", v))}
            />
          </PanelSectionRow>
        ) : null}
        <PanelSectionRow>
          <ToggleField
            label="Mute game sound while talking"
            description={
              s.settings.mute
                ? "The game goes quiet while the mic listens, so its sound does not confuse the speech recognition. Start talking when it goes quiet."
                : "The game keeps playing while you talk. Game sound can cause wrong words."
            }
            checked={s.settings.mute}
            disabled={busy}
            onChange={(v) => act(setSetting("mute", v))}
          />
        </PanelSectionRow>
        <PanelSectionRow>
          <ToggleField
            label="Show recording symbol"
            description={
              s.settings.indicator
                ? "A microphone at the top of the screen: grey = wait, red = talk now, dots = working, green = done."
                : "No symbol on screen while you talk."
            }
            checked={s.settings.indicator}
            disabled={busy}
            onChange={(v) => act(setSetting("indicator", v))}
          />
        </PanelSectionRow>
      </PanelSection>

      <PanelSection title="Push-to-talk buttons">
        <PanelSectionRow>
          <Field label="Buttons" focusable highlightOnFocus
            description={<span style={small}>{combo}</span>} />
        </PanelSectionRow>
        <PanelSectionRow>
          <ButtonItem layout="below" disabled={busy || choosing}
            onClick={() => {
              fast.current = true;
              act(captureButtons());
            }}>
            Change buttons
          </ButtonItem>
        </PanelSectionRow>
        {choosing ? (
          <PanelSectionRow>
            <Field label={heldLabel(s.capture)} focusable highlightOnFocus
              description={<span style={small}>{heldText(s.capture)}</span>} />
          </PanelSectionRow>
        ) : s.capture.message ? (
          <PanelSectionRow>
            <Field label={s.capture.state === "done" ? "✔ Saved" : "✖ Not changed"} focusable highlightOnFocus
              description={<span style={small}>{s.capture.message}</span>} />
          </PanelSectionRow>
        ) : null}
        {!isDefaultCombo && !choosing ? (
          <PanelSectionRow>
            <ButtonItem layout="below" disabled={busy} onClick={() => act(setSetting("combo", DEFAULT_COMBO))}>
              Reset to {DEFAULT_COMBO.join("+")}
            </ButtonItem>
          </PanelSectionRow>
        ) : null}
      </PanelSection>

      {s.channel === "dev" ? (
        <PanelSection title="Testing">
          <PanelSectionRow>
            <ToggleField
              label="Detailed log"
              description={
                s.settings.detailed_log
                  ? "Every step, including what you said, is written to the plugin's log file (Decky keeps the last 5)."
                  : "Only errors are written to the log file."
              }
              checked={s.settings.detailed_log}
              disabled={busy}
              onChange={(v) => act(setSetting("detailed_log", v))}
            />
          </PanelSectionRow>
        </PanelSection>
      ) : null}

      <PanelSection title="Self-test">
        <PanelSectionRow>
          <ButtonItem layout="below" disabled={busy} onClick={runSelfTest}>
            Check this Deck
          </ButtonItem>
        </PanelSectionRow>
        {(checks ?? []).map(([label, ok, detail], i) => (
          <PanelSectionRow key={`c${i}`}>
            <Field label={`${ok ? "✔" : "✖"} ${label}`} focusable highlightOnFocus
              description={<span style={small}>{detail}</span>} />
          </PanelSectionRow>
        ))}
      </PanelSection>

      <PanelSection title="Activity (newest last)">
        {s.log.length === 0 ? (
          <PanelSectionRow>
            <Field focusable description={<span style={small}>No activity yet</span>} />
          </PanelSectionRow>
        ) : (
          s.log.map((line, i) => (
            <PanelSectionRow key={`l${i}`}>
              <Field focusable padding="compact" bottomSeparator="none"
                description={<span style={{ ...small, fontSize: "11px" }}>{line}</span>} />
            </PanelSectionRow>
          ))
        )}
      </PanelSection>
    </>
  );
}

function MicIcon() {
  return (
    <svg width="1em" height="1em" viewBox="0 0 24 24" fill="currentColor" aria-hidden="true">
      <path d="M12 14a3 3 0 0 0 3-3V5a3 3 0 0 0-6 0v6a3 3 0 0 0 3 3zm5-3a5 5 0 0 1-10 0H5a7 7 0 0 0 6 6.92V21h2v-3.08A7 7 0 0 0 19 11h-2z" />
    </svg>
  );
}

export default definePlugin(() => ({
  name: "Rootless Dictation (dev)",
  titleView: <div className={staticClasses.Title}>Rootless Dictation</div>,
  content: <Content />,
  icon: <MicIcon />,
}));
