const STAGE0_SOURCE_MARKER = "stage0-script-search-marker";

function stage0RequestBuilder() {
  return {
    marker: "stage0-request",
    count: 3,
    sourceMarker: STAGE0_SOURCE_MARKER,
  };
}

function buildStatefulStreamRequest() {
  return {
    stream_id: "stateful-stream-fixture",
    variant: "fixture-variant",
    events: [
      {
        id: "client-event-1",
        actor: { kind: "client" },
        payload: {
          type: "text",
          parts: ["hello fixture"],
        },
      },
    ],
    parent_event_id: "root-event",
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
    events.push(data === "[DONE]" ? data : JSON.parse(data));
  }
  return events;
}

async function sendStatefulStream() {
  const response = await fetch("/api/stateful-stream", {
    method: "POST",
    headers: {
      "Authorization": "Bearer fixture-token",
      "Content-Type": "application/json",
    },
    credentials: "include",
    body: JSON.stringify(buildStatefulStreamRequest()),
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
    body: JSON.stringify(stage0RequestBuilder()),
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

    source.onmessage = (event) => {
      events.push(event.data);
    };
    source.addEventListener("done", (event) => {
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
document.querySelector("#send-stateful-stream").addEventListener("click", sendStatefulStream);
