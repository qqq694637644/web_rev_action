const SYNTHETIC_SOURCE_MARKER = "synthetic-script-search-marker";
const TERMINAL_MARKER = "fixture-complete";

function buildEchoRequest() {
  return {
    marker: "synthetic-request",
    count: 3,
    sourceMarker: SYNTHETIC_SOURCE_MARKER,
  };
}

function buildAuthenticatedStreamRequest() {
  return {
    job_id: "authenticated-stream-fixture",
    profile: "fixture-profile",
    records: [
      {
        record_id: "client-record-1",
        source: { kind: "client" },
        content: {
          format: "text",
          segments: ["hello fixture"],
        },
      },
    ],
    cursor_id: "root-cursor",
    timezone_offset_min: 0,
    tracking_id: "tracking-only-value",
  };
}

function parseSseText(text) {
  const events = [];
  for (const block of text.split(/\r?\n\r?\n/)) {
    const data = block
      .split(/\r?\n/)
      .filter((line) => line.startsWith("data:"))
      .map((line) => line.slice(5).trim())
      .join("\n");
    if (!data) continue;
    events.push(data === TERMINAL_MARKER ? data : JSON.parse(data));
  }
  return events;
}

async function sendAuthenticatedStream() {
  const response = await fetch("/api/stateful-stream", {
    method: "POST",
    headers: {
      "Authorization": "Bearer fixture-token",
      "Content-Type": "application/json",
    },
    credentials: "include",
    body: JSON.stringify(buildAuthenticatedStreamRequest()),
  });
  const contentType = response.headers.get("content-type") || "";
  const payload = contentType.includes("text/event-stream")
    ? parseSseText(await response.text())
    : await response.json();
  document.querySelector("#result").textContent = JSON.stringify(payload, null, 2);
  document.querySelector("#status").textContent = `stateful-stream-${response.status}`;
  return payload;
}

async function sendEcho() {
  const response = await fetch("/api/echo", {
    method: "POST",
    headers: {
      "Content-Type": "application/json",
    },
    body: JSON.stringify(buildEchoRequest()),
  });
  if (!response.ok) {
    throw new Error(`Echo request failed: ${response.status}`);
  }
  return response.json();
}

function collectSse() {
  return new Promise((resolve, reject) => {
    const events = [];
    const source = new EventSource("/api/sse");

    source.addEventListener("chunk", (event) => {
      events.push(event.data);
    });
    source.addEventListener("complete", (event) => {
      events.push(event.data);
      source.close();
      resolve(events);
    });
    source.onerror = () => {
      source.close();
      reject(new Error("SSE stream failed"));
    };
  });
}

async function runCapture() {
  const status = document.querySelector("#status");
  const result = document.querySelector("#result");
  status.textContent = "capture-running";
  try {
    const [echo, sse] = await Promise.all([sendEcho(), collectSse()]);
    result.textContent = JSON.stringify({ echo, sse }, null, 2);
    status.textContent = "capture-complete";
  } catch (error) {
    result.textContent = String(error);
    status.textContent = "capture-failed";
  }
}

document.querySelector("#run-capture").addEventListener("click", runCapture);
document.querySelector("#send-echo").addEventListener("click", sendEcho);
document.querySelector("#send-stateful-stream").addEventListener("click", sendAuthenticatedStream);
