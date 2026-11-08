const HEALTH_STATUS_CLASSES = {
    ok: "health-dot-ok",
    degraded: "health-dot-degraded",
    down: "health-dot-down",
    unknown: "health-dot-unknown",
}

const HEALTH_POLL_INTERVAL_MS = 5 * 60 * 1000

async function fetchHealthStatus() {
    try {
        const resp = await fetch(`${window.ROOT_PATH}/health-status`)
        if (!resp.ok) return null
        return await resp.json()
    } catch (_) {
        return null
    }
}

function renderHealthStatus(data) {
    const bar = document.getElementById("health-status-bar")
    const dot = document.getElementById("health-dot")
    const text = document.getElementById("health-text")
    if (!data) {
        bar.style.display = "none"
        return
    }
    const cls = HEALTH_STATUS_CLASSES[data.overall] || HEALTH_STATUS_CLASSES.unknown
    dot.className = "health-dot " + cls
    text.textContent = data.summary
    bar.style.display = ""
}

async function pollHealthStatus() {
    renderHealthStatus(await fetchHealthStatus())
}

document.addEventListener("DOMContentLoaded", () => {
    pollHealthStatus()
    setInterval(pollHealthStatus, HEALTH_POLL_INTERVAL_MS)
})
