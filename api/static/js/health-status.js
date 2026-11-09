const HEALTH_DOT_CLASSES = {
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
    const items = document.getElementById("health-status-items")
    if (!bar || !items) return

    if (!data || data.overall === "unknown" || !data.areas || data.areas.length === 0) {
        const summary = (data && data.summary) ? data.summary : "Status unavailable"
        items.innerHTML = `
            <div class="health-item">
                <span class="health-dot health-dot-unknown"></span>
                <span class="health-item-name">System Status</span>
                <span class="health-item-summary">${summary}</span>
            </div>`
        return
    }

    items.innerHTML = data.areas.map(area => `
        <div class="health-item">
            <span class="health-dot ${HEALTH_DOT_CLASSES[area.status] || "health-dot-unknown"}"></span>
            <span class="health-item-name">${area.name}</span>
            <span class="health-item-summary">${area.summary}</span>
        </div>`
    ).join("")
}

async function pollHealthStatus() {
    renderHealthStatus(await fetchHealthStatus())
}

document.addEventListener("DOMContentLoaded", () => {
    pollHealthStatus()
    setInterval(pollHealthStatus, HEALTH_POLL_INTERVAL_MS)
})
