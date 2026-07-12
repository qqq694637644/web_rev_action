const STAGE0_SOURCE_MARKER = "stage0-script-search-marker";

function stage0RequestBuilder() {
  return {
    marker: "stage0-request",
    count: 3,
    sourceMarker: STAGE0_SOURCE_MARKER,
  };
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
