"use strict";

const API_ROLE = "operator";
const POLL_INTERVAL_MS = 2000;
const LOW_LIMIT_C = 2.0;
const HIGH_LIMIT_C = 8.0;
const CUSTODY_STAGES = [
  "Manufacturer",
  "Vehicle",
  "Regional store",
  "Last mile",
  "Clinic",
];

const SERIES_COLORS = ["#f87171", "#60a5fa", "#34d399", "#fbbf24", "#c084fc"];

let focusConsignmentId = "CN-0417";
let lastReadingTsById = {};
let readingsById = {};
let tempChart = null;

function apiHeaders() {
  return { "X-Role": API_ROLE };
}

async function apiGet(path) {
  const response = await fetch(path, { headers: apiHeaders() });
  if (!response.ok) {
    throw new Error(`${path} returned ${response.status}`);
  }
  return response.json();
}

function formatTime(ts) {
  if (ts === null || ts === undefined) {
    return "";
  }
  const date = new Date(ts * 1000);
  return date.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", second: "2-digit" });
}

function actionClass(action) {
  const normalized = String(action).toLowerCase();
  if (normalized === "quarantine") return "quarantine";
  if (normalized === "investigate") return "investigate";
  return "monitor";
}

function buildExcursionAnnotations(readings, alerts) {
  const annotations = {};

  annotations.lowLine = {
    type: "line",
    yMin: LOW_LIMIT_C,
    yMax: LOW_LIMIT_C,
    borderColor: "rgba(59, 130, 246, 0.8)",
    borderWidth: 1,
    borderDash: [6, 4],
    label: {
      display: true,
      content: "2 °C",
      position: "start",
      backgroundColor: "transparent",
      color: "#8b9cb3",
      font: { size: 10 },
    },
  };

  annotations.highLine = {
    type: "line",
    yMin: HIGH_LIMIT_C,
    yMax: HIGH_LIMIT_C,
    borderColor: "rgba(59, 130, 246, 0.8)",
    borderWidth: 1,
    borderDash: [6, 4],
    label: {
      display: true,
      content: "8 °C",
      position: "start",
      backgroundColor: "transparent",
      color: "#8b9cb3",
      font: { size: 10 },
    },
  };

  if (!readings.length) {
    return annotations;
  }

  const excursionAlerts = alerts.filter((alert) => alert.type === "EXCURSION");
  const lastTs = readings[readings.length - 1].ts;
  let boxIndex = 0;

  for (const alert of excursionAlerts) {
    const startTs = alert.ts;
    const endTs = lastTs;
    if (endTs <= startTs) {
      continue;
    }
    annotations[`excursion${boxIndex}`] = {
      type: "box",
      xMin: startTs,
      xMax: endTs,
      backgroundColor: "rgba(239, 68, 68, 0.18)",
      borderWidth: 0,
    };
    boxIndex += 1;
  }

  if (boxIndex === 0) {
    let inExcursion = false;
    let excursionStart = null;
    for (const reading of readings) {
      const outside = reading.temp_c < LOW_LIMIT_C || reading.temp_c > HIGH_LIMIT_C;
      if (outside && !inExcursion) {
        inExcursion = true;
        excursionStart = reading.ts;
      } else if (!outside && inExcursion) {
        annotations[`tempExcursion${boxIndex}`] = {
          type: "box",
          xMin: excursionStart,
          xMax: reading.ts,
          backgroundColor: "rgba(239, 68, 68, 0.18)",
          borderWidth: 0,
        };
        boxIndex += 1;
        inExcursion = false;
        excursionStart = null;
      }
    }
    if (inExcursion && excursionStart !== null) {
      annotations[`tempExcursion${boxIndex}`] = {
        type: "box",
        xMin: excursionStart,
        xMax: lastTs,
        backgroundColor: "rgba(239, 68, 68, 0.18)",
        borderWidth: 0,
      };
    }
  }

  return annotations;
}

function averageByTimestamp(readings) {
  const buckets = new Map();
  for (const reading of readings) {
    const existing = buckets.get(reading.ts);
    if (existing) {
      existing.total += reading.temp_c;
      existing.count += 1;
    } else {
      buckets.set(reading.ts, { total: reading.temp_c, count: 1 });
    }
  }

  return Array.from(buckets.entries())
    .sort((left, right) => left[0] - right[0])
    .map(([ts, bucket]) => ({
      ts,
      temp_c: bucket.total / bucket.count,
    }));
}

function mergeReadings(existing, incoming) {
  const merged = [...existing];
  for (const reading of incoming) {
    if (!merged.find((item) => item.ts === reading.ts && item.device_id === reading.device_id)) {
      merged.push(reading);
    }
  }
  merged.sort((left, right) => left.ts - right.ts);
  return merged;
}

function colorForIndex(index) {
  return SERIES_COLORS[index % SERIES_COLORS.length];
}

function hexToRgba(hex, alpha) {
  const value = hex.replace("#", "");
  const red = parseInt(value.slice(0, 2), 16);
  const green = parseInt(value.slice(2, 4), 16);
  const blue = parseInt(value.slice(4, 6), 16);
  return `rgba(${red}, ${green}, ${blue}, ${alpha})`;
}

function initChart() {
  const canvas = document.getElementById("temp-chart");
  tempChart = new Chart(canvas, {
    type: "line",
    data: { datasets: [] },
    options: {
      responsive: true,
      maintainAspectRatio: false,
      animation: false,
      scales: {
        x: {
          type: "linear",
          ticks: {
            color: "#8b9cb3",
            callback: (value) => formatTime(value),
            maxTicksLimit: 8,
          },
          grid: { color: "rgba(45, 58, 79, 0.6)" },
        },
        y: {
          min: 0,
          max: 16,
          ticks: { color: "#8b9cb3" },
          grid: { color: "rgba(45, 58, 79, 0.6)" },
          title: {
            display: true,
            text: "°C",
            color: "#8b9cb3",
          },
        },
      },
      plugins: {
        legend: {
          display: true,
          labels: {
            color: "#e8edf4",
            boxWidth: 12,
            padding: 16,
          },
        },
        annotation: { annotations: {} },
      },
    },
  });
}

function updateChart(consignments, alerts) {
  if (!tempChart) {
    initChart();
  }

  const datasets = consignments.map((consignment, index) => {
    const color = colorForIndex(index);
    const series = averageByTimestamp(readingsById[consignment.id] || []);
    return {
      label: consignment.id,
      data: series,
      borderColor: color,
      backgroundColor: hexToRgba(color, 0.08),
      borderWidth: consignment.id === focusConsignmentId ? 2.5 : 2,
      pointRadius: 0,
      pointHitRadius: 6,
      tension: 0.15,
      parsing: {
        xAxisKey: "ts",
        yAxisKey: "temp_c",
      },
    };
  });

  tempChart.data.datasets = datasets;

  const allTemps = datasets.flatMap((dataset) => dataset.data.map((point) => point.temp_c));
  if (allTemps.length) {
    tempChart.options.scales.y.max = Math.max(16, Math.ceil(Math.max(...allTemps) + 2));
  }

  const focusReadings = averageByTimestamp(readingsById[focusConsignmentId] || []);
  const focusAlerts = alerts.filter((alert) => alert.consignment_id === focusConsignmentId);
  tempChart.options.plugins.annotation.annotations = buildExcursionAnnotations(
    focusReadings,
    focusAlerts,
  );
  tempChart.update("none");
}

function updateKpis(consignments, alerts, devices) {
  const activeCount = consignments.filter((item) => item.status === "ACTIVE").length;
  const excursionCount = alerts.filter((alert) => alert.type === "EXCURSION").length;
  const offlineCount = devices.filter((device) => !device.online).length;

  let dosesAtRisk = 0;
  for (const consignment of consignments) {
    const atRisk =
      consignment.disposition === "QUARANTINE" ||
      consignment.disposition === "INVESTIGATE" ||
      alerts.some(
        (alert) =>
          alert.consignment_id === consignment.id && alert.type === "EXCURSION",
      );
    if (atRisk) {
      dosesAtRisk += consignment.doses;
    }
  }

  document.getElementById("kpi-active").textContent = String(activeCount);
  document.getElementById("kpi-excursion").textContent = String(excursionCount);
  document.getElementById("kpi-offline").textContent = String(offlineCount);
  document.getElementById("kpi-doses").textContent = dosesAtRisk.toLocaleString();
}

function updateAlerts(alerts) {
  const list = document.getElementById("alert-list");
  list.innerHTML = "";

  if (!alerts.length) {
    const empty = document.createElement("li");
    empty.className = "empty-state";
    empty.textContent = "No open alerts";
    list.appendChild(empty);
    return;
  }

  for (const alert of alerts) {
    const item = document.createElement("li");
    const severityClass = alert.severity === "CRITICAL" ? "critical" : "warn";
    item.className = `alert-item ${severityClass}`;

    const header = document.createElement("div");
    header.className = "alert-header";

    const idSpan = document.createElement("span");
    idSpan.className = "alert-id";
    idSpan.textContent = alert.consignment_id;

    const actionSpan = document.createElement("span");
    actionSpan.className = `action-label ${actionClass(alert.action)}`;
    actionSpan.textContent = alert.action;

    header.appendChild(idSpan);
    header.appendChild(actionSpan);

    const desc = document.createElement("div");
    desc.className = "alert-desc";
    desc.textContent = alert.description;

    item.appendChild(header);
    item.appendChild(desc);
    list.appendChild(item);
  }
}

function updateCustodyChain(readings) {
  const nodes = document.querySelectorAll(".custody-node");
  for (const node of nodes) {
    node.classList.remove("active", "complete");
  }

  if (!readings.length) {
    nodes[0].classList.add("active");
    return;
  }

  const firstTs = readings[0].ts;
  const lastTs = readings[readings.length - 1].ts;
  const journeyDurationS = 12 * 3600;
  const progress = Math.min(1, Math.max(0, (lastTs - firstTs) / journeyDurationS));
  const stageIndex = Math.min(CUSTODY_STAGES.length - 1, Math.floor(progress * CUSTODY_STAGES.length));

  for (let index = 0; index < nodes.length; index += 1) {
    if (index < stageIndex) {
      nodes[index].classList.add("complete");
    } else if (index === stageIndex) {
      nodes[index].classList.add("active");
    }
  }
}

function pickFocusConsignment(consignments, alerts) {
  const excursion = alerts.find((alert) => alert.type === "EXCURSION");
  if (excursion && consignments.some((item) => item.id === excursion.consignment_id)) {
    return excursion.consignment_id;
  }
  if (consignments.some((item) => item.id === focusConsignmentId)) {
    return focusConsignmentId;
  }
  return consignments[0] ? consignments[0].id : focusConsignmentId;
}

async function poll() {
  try {
    const [consignments, devices, alerts] = await Promise.all([
      apiGet("/api/consignments"),
      apiGet("/api/devices"),
      apiGet("/api/alerts"),
    ]);

    if (consignments.length) {
      focusConsignmentId = pickFocusConsignment(consignments, alerts);
    }

    document.getElementById("chart-consignment").textContent = consignments.length
      ? `${consignments.length} consignments`
      : "no consignments";

    await Promise.all(
      consignments.map(async (consignment) => {
        const since = lastReadingTsById[consignment.id];
        const readingsPath =
          since === undefined
            ? `/api/consignments/${consignment.id}/readings`
            : `/api/consignments/${consignment.id}/readings?since=${since}`;
        const incoming = await apiGet(readingsPath);
        const existing = readingsById[consignment.id] || [];
        const merged = since === undefined ? incoming : mergeReadings(existing, incoming);
        readingsById[consignment.id] = merged;
        if (merged.length) {
          lastReadingTsById[consignment.id] = merged[merged.length - 1].ts;
        }
        return merged;
      }),
    );

    const focusReadings = readingsById[focusConsignmentId] || [];

    updateKpis(consignments, alerts, devices);
    updateAlerts(alerts);
    updateChart(consignments, alerts);
    updateCustodyChain(focusReadings);

    if (consignments.length) {
      const thermal = await apiGet(`/api/consignments/${focusConsignmentId}/thermal`);
      document.getElementById("status-bar").textContent =
        `${focusConsignmentId} · MKT ${thermal.mkt_c ?? "—"} °C · ` +
        `${thermal.consumed_dm} / ${thermal.budget_dm} degree-min · ` +
        `Disposition ${thermal.disposition}`;
    }
    document.getElementById("last-updated").textContent =
      `Updated ${new Date().toLocaleTimeString()}`;
  } catch (error) {
    console.error("Dashboard poll failed:", error);
    document.getElementById("status-bar").textContent = `Poll error: ${error.message}`;
  }
}

document.addEventListener("DOMContentLoaded", () => {
  initChart();
  poll();
  setInterval(poll, POLL_INTERVAL_MS);
});
