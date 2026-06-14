# JBE-Company-Watcher

## Features

- Company News Tracking
- Employee Activity Monitoring
- Addiction Monitoring
- Low Stock Alerts
- Company Role Mapping
- Automatic Role Assignment via YATA Verification
- Custom Alert Channels
- Per-Company Threshold Configuration

## Commands

### `/add_company`
Add a company to track using its Company ID and a custom API key.

**Options:**
- Company ID
- Custom API Key
- News Channel (optional)
- Alert Channel (optional)

> News and alerts can be sent to the same Discord channel if desired.

---

### `/set_channel`
Configure where company news and alerts are posted.

**Parameters:**
- Company ID
- News Channel
- Alert Channel

Use this command at any time to update channel assignments.

---

### `/set_thresholds`
Configure activity and addiction alert thresholds for a company.

**Parameters:**
- Company ID
- Activity Threshold
- Addiction Threshold

If no custom thresholds are configured, the bot will use the default values.

---

### `/stock_rule_set`
Configure stock monitoring and low-stock alerts.

**Parameters:**
- Company ID

The command will display all company stock items and allow you to set custom alert amounts for each item.

**Example Uses:**
- Alert when Clarinets fall below 1,000
- Alert when Dinner Candles fall below 25,000
- Alert when Salt fall below 2,500

---

### `/map_company_role`
Automatically assign Discord roles based on a member's company.

**Parameters:**
- Company ID
- Discord Role

When a member verifies through YATA and is employed by the mapped company, the configured role will be assigned automatically.

**Features:**
- Automatic role assignment
- Automatic role removal when leaving the company
- Works with YATA verification
