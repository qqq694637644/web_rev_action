async ({localFile}) => {
          const spec = JSON.parse(localFile.text);
          const headers = new Headers();
          for (const entry of spec.headers || []) {
            headers.append(String(entry.name), String(entry.value));
          }
          let body;
          if (spec.body && spec.body.encoding === 'utf8') {
            body = spec.body.text;
          } else if (spec.body && spec.body.encoding === 'base64') {
            const binary = atob(spec.body.base64 || '');
            const bytes = new Uint8Array(binary.length);
            for (let i = 0; i < binary.length; i++) bytes[i] = binary.charCodeAt(i);
            body = bytes;
          }
          const transport = spec.transport || {};
          const response = await fetch(spec.url, {
            method: spec.method,
            headers,
            body: ['GET', 'HEAD'].includes(String(spec.method).toUpperCase()) ? undefined : body,
            credentials: transport.credentials || 'include',
            redirect: transport.redirect || 'follow',
            cache: transport.cache || 'default',
            referrerPolicy: transport.referrer_policy || '',
            keepalive: Boolean(transport.keepalive),
            mode: transport.mode || 'cors',
            priority: transport.priority || 'auto',
          });
          const responseControl = spec.responseControl || {};
          const maxResponseBytes = Math.max(
            8192,
            Number(responseControl.maxResponseBytes || 8 * 1024 * 1024),
          );
          const maxEvents = Math.max(
            1,
            Number(responseControl.maxEvents || 10000),
          );
          const idleWindowMs = responseControl.idleWindowMs == null
            ? null
            : Math.max(10, Number(responseControl.idleWindowMs));
          const configuredMode = String(responseControl.responseMode || 'auto');
          const responseHeaderEntries = Array.from(response.headers.entries());
          const contentTypeHeader = response.headers.get
            ? response.headers.get('content-type')
            : (responseHeaderEntries.find(
                ([name]) => String(name).toLowerCase() === 'content-type',
              ) || [null, ''])[1];
          const contentType = String(contentTypeHeader || '')
            .split(';', 1)[0]
            .trim()
            .toLowerCase();
          const responseMode = configuredMode === 'auto'
            ? contentType === 'text/event-stream'
              ? 'sse'
              : ['application/x-ndjson', 'application/ndjson'].includes(contentType)
                ? 'ndjson'
                : 'ordinary'
            : configuredMode;
          const terminalConditions = Array.isArray(responseControl.terminalConditions)
            ? responseControl.terminalConditions
            : [];
          const exactSseCondition = terminalConditions.find(
            (item) => item && item.type === 'exact_sse_data',
          );
          const textPatternConditions = terminalConditions.filter(
            (item) => item && item.type === 'text_pattern' && item.value != null,
          );
          const doneMarker = exactSseCondition && exactSseCondition.value != null
            ? String(exactSseCondition.value)
            : responseControl.doneMarker == null
              ? null
              : String(responseControl.doneMarker);
          const doneEventName = exactSseCondition && exactSseCondition.event_name != null
            ? String(exactSseCondition.event_name)
            : responseControl.doneEventName == null
              ? null
              : String(responseControl.doneEventName);
          const reader = response.body ? response.body.getReader() : null;
          const decoder = new TextDecoder('utf-8', {fatal: false});
          const previewChunks = [];
          let previewByteLength = 0;
          let bodyByteLength = 0;
          let doneMarkerObserved = false;
          let doneEventNameObserved = null;
          let terminalConditionMatched = null;
          let truncated = false;
          let terminationReason = reader ? 'network_close' : 'no_response_body';
          let sseLineBuffer = '';
          let sseEventName = 'message';
          let sseDataLines = [];
          let textPatternBuffer = '';
          let ndjsonLineBuffer = '';
          let ndjsonRecordCount = 0;
          let ndjsonParseErrorCount = 0;
          let sseEventCount = 0;
          let rawChunkCount = 0;
          const ndjsonRecordMetadata = [];
          const chunkBoundaries = [];
          const consumeNdjson = (text, eof) => {
            ndjsonLineBuffer += text;
            const lines = ndjsonLineBuffer.split('\n');
            ndjsonLineBuffer = eof ? '' : lines.pop() || '';
            if (eof && ndjsonLineBuffer) lines.push(ndjsonLineBuffer);
            for (const rawLine of lines) {
              if (ndjsonRecordCount >= maxEvents) return true;
              const line = rawLine.endsWith('\r') ? rawLine.slice(0, -1) : rawLine;
              if (!line.trim()) continue;
              let valid = true;
              try { JSON.parse(line); }
              catch { valid = false; ndjsonParseErrorCount += 1; }
              if (ndjsonRecordMetadata.length < 256) {
                ndjsonRecordMetadata.push({
                  index: ndjsonRecordCount,
                  valid,
                  byteLength: new TextEncoder().encode(line).byteLength,
                });
              }
              ndjsonRecordCount += 1;
              if (ndjsonRecordCount >= maxEvents) return true;
            }
            return false;
          };
          const dispatchSseEvent = () => {
            const data = sseDataLines.join('\n');
            const eventName = sseEventName;
            const hasEvent = sseDataLines.length > 0 || eventName !== 'message';
            sseEventName = 'message';
            sseDataLines = [];
            if (!hasEvent) return null;
            sseEventCount += 1;
            if (
              doneMarker &&
              data === doneMarker &&
              (!doneEventName || eventName === doneEventName)
            ) {
              doneEventNameObserved = eventName;
              return 'done_marker';
            }
            if (sseEventCount >= maxEvents) return 'max_events';
            return null;
          };
          const consumeSseLine = (line) => {
            if (line === '') return dispatchSseEvent();
            if (line.startsWith(':')) return null;
            const colon = line.indexOf(':');
            const field = colon < 0 ? line : line.slice(0, colon);
            let fieldValue = colon < 0 ? '' : line.slice(colon + 1);
            if (fieldValue.startsWith(' ')) fieldValue = fieldValue.slice(1);
            if (field === 'event') sseEventName = fieldValue;
            if (field === 'data') sseDataLines.push(fieldValue);
            return null;
          };
          const consumeSseEvents = (text, eof = false) => {
            sseLineBuffer += text;
            let offset = 0;
            while (offset < sseLineBuffer.length) {
              let separator = -1;
              for (let i = offset; i < sseLineBuffer.length; i++) {
                const code = sseLineBuffer.charCodeAt(i);
                if (code === 10 || code === 13) {
                  separator = i;
                  break;
                }
              }
              if (separator < 0) break;
              if (
                sseLineBuffer.charCodeAt(separator) === 13 &&
                separator + 1 === sseLineBuffer.length &&
                !eof
              ) {
                break;
              }
              const line = sseLineBuffer.slice(offset, separator);
              let next = separator + 1;
              if (
                sseLineBuffer.charCodeAt(separator) === 13 &&
                sseLineBuffer.charCodeAt(separator + 1) === 10
              ) {
                next += 1;
              }
              offset = next;
              const terminalReason = consumeSseLine(line);
              if (terminalReason) {
                sseLineBuffer = sseLineBuffer.slice(offset);
                return terminalReason;
              }
            }
            sseLineBuffer = sseLineBuffer.slice(offset);
            if (eof) {
              if (sseLineBuffer.length > 0) {
                const terminalReason = consumeSseLine(sseLineBuffer);
                if (terminalReason) {
                  sseLineBuffer = '';
                  return terminalReason;
                }
                sseLineBuffer = '';
              }
              if (sseDataLines.length > 0 || sseEventName !== 'message') {
                return dispatchSseEvent();
              }
            }
            return null;
          };
          const readWithIdleWindow = async () => {
            if (idleWindowMs == null) return await reader.read();
            let timer;
            try {
              return await Promise.race([
                reader.read(),
                new Promise((_, reject) => {
                  timer = setTimeout(
                    () => reject(new Error('__REPLAY_IDLE_WINDOW__')),
                    idleWindowMs,
                  );
                }),
              ]);
            } finally {
              if (timer) clearTimeout(timer);
            }
          };
          if (reader) {
            while (true) {
              let readResult;
              try {
                readResult = await readWithIdleWindow();
              } catch (error) {
                if (String(error && error.message) !== '__REPLAY_IDLE_WINDOW__') throw error;
                terminalConditionMatched = 'idle_window';
                terminationReason = 'idle_window';
                await reader.cancel('idle_window').catch(() => {});
                break;
              }
              if (readResult.done) {
                const finalText = decoder.decode();
                const ndjsonLimitReached = responseMode === 'ndjson'
                  ? consumeNdjson(finalText, true)
                  : false;
                const sseTermination = responseMode === 'sse'
                  ? consumeSseEvents(finalText, true)
                  : null;
                if (ndjsonLimitReached) {
                  terminalConditionMatched = 'max_events';
                  terminationReason = 'max_events';
                } else if (sseTermination) {
                  doneMarkerObserved = sseTermination === 'done_marker';
                  terminalConditionMatched = doneMarkerObserved
                    ? 'exact_sse_data'
                    : 'max_events';
                  terminationReason = sseTermination;
                } else {
                  terminalConditionMatched = terminalConditions.some(
                    (item) => item && item.type === 'network_close',
                  ) ? 'network_close' : null;
                  terminationReason = 'network_close';
                }
                break;
              }
              const chunk = readResult.value || new Uint8Array();
              const remaining = maxResponseBytes - bodyByteLength;
              const overflow = chunk.byteLength > remaining;
              const accepted = chunk.subarray(
                0,
                Math.min(chunk.byteLength, Math.max(0, remaining)),
              );
              const chunkStart = bodyByteLength;
              bodyByteLength += accepted.byteLength;
              if (accepted.byteLength > 0 && chunkBoundaries.length < 4096) {
                chunkBoundaries.push({
                  index: chunkBoundaries.length,
                  byteStart: chunkStart,
                  byteEnd: bodyByteLength,
                  byteLength: accepted.byteLength,
                });
              }
              if (previewByteLength < 8192) {
                const previewPart = accepted.subarray(
                  0,
                  Math.min(accepted.byteLength, 8192 - previewByteLength),
                );
                previewChunks.push(previewPart);
                previewByteLength += previewPart.byteLength;
              }
              const decodedText = decoder.decode(accepted, {stream: true});
              if (responseMode === 'ndjson') {
                if (consumeNdjson(decodedText, false)) {
                  terminalConditionMatched = 'max_events';
                  terminationReason = 'max_events';
                  await reader.cancel('max_events').catch(() => {});
                  break;
                }
              }
              if (responseMode === 'sse') {
                const sseTermination = consumeSseEvents(decodedText, false);
                if (sseTermination) {
                  doneMarkerObserved = sseTermination === 'done_marker';
                  terminalConditionMatched = doneMarkerObserved
                    ? 'exact_sse_data'
                    : 'max_events';
                  terminationReason = sseTermination;
                  await reader.cancel(sseTermination).catch(() => {});
                  break;
                }
              }
              if (textPatternConditions.length > 0) {
                textPatternBuffer = (textPatternBuffer + decodedText).slice(-65536);
                const matchedPattern = textPatternConditions.find(
                  (item) => textPatternBuffer.includes(String(item.value)),
                );
                if (matchedPattern) {
                  terminalConditionMatched = 'text_pattern';
                  terminationReason = 'text_pattern';
                  await reader.cancel('text_pattern').catch(() => {});
                  break;
                }
              }
              if (responseMode === 'raw_stream' && accepted.byteLength > 0) {
                rawChunkCount += 1;
                if (rawChunkCount >= maxEvents) {
                  terminalConditionMatched = 'max_events';
                  terminationReason = 'max_events';
                  await reader.cancel('max_events').catch(() => {});
                  break;
                }
              }
              if (overflow) {
                truncated = true;
                terminationReason = 'max_response_bytes';
                await reader.cancel('max_response_bytes').catch(() => {});
                break;
              }
            }
          }
          const previewBytes = new Uint8Array(previewByteLength);
          let previewOffset = 0;
          for (const chunk of previewChunks) {
            previewBytes.set(chunk, previewOffset);
            previewOffset += chunk.byteLength;
          }
          let preview = '';
          try { preview = new TextDecoder('utf-8', {fatal: false}).decode(previewBytes); }
          catch { preview = ''; }
          return {
            status: response.status,
            statusText: response.statusText,
            url: response.url,
            redirected: response.redirected,
            ok: response.ok,
            headers: responseHeaderEntries,
            bodyByteLength,
            bodyPreview: preview,
            doneMarkerObserved,
            doneEventNameObserved,
            responseMode,
            chunkBoundaries,
            ndjsonRecordCount,
            ndjsonParseErrorCount,
            ndjsonRecordMetadata,
            sseEventCount,
            rawChunkCount,
            streamEventCount: responseMode === 'sse'
              ? sseEventCount
              : responseMode === 'ndjson'
                ? ndjsonRecordCount
                : responseMode === 'raw_stream'
                  ? rawChunkCount
                  : 0,
            terminalConditionMatched,
            terminationReason,
            truncated,
            maxResponseBytes,
            maxEvents,
            idleWindowMs,
          };
        }
