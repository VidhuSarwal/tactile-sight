# TactileSight — WebSocket Data Integration

The server at `http://10.221.208.1:8081` exposes a WebSocket on port 8083 that pushes a
snapshot bundle each time the capture button is pressed.

## Connecting

```javascript
const ws = new WebSocket('ws://10.221.208.1:8083');
ws.onopen  = () => console.log('connected');
ws.onclose = () => setTimeout(connect, 2000); // auto-reconnect
ws.onmessage = (e) => handleBundle(JSON.parse(e.data));
```

## Bundle format

Each message is a JSON string:

```json
{
  "ts":        1721234567.123,
  "rgb_b64":   "<base64-encoded JPEG>",
  "depth_b64": "<base64-encoded PNG>"
}
```

| Field | Type | Description |
|-------|------|-------------|
| `ts` | float | Unix timestamp (seconds) of capture |
| `rgb_b64` | string | Base64 JPEG, 640×480, RGB colour frame |
| `depth_b64` | string | Base64 PNG, 640×480, **16-bit grayscale**, values in millimetres |

## Triggering a capture

POST to `/capture` — the server grabs the current depth frame + RGB frame and broadcasts the bundle to all connected WebSocket clients.

```bash
curl -X POST http://10.221.208.1:8081/capture
# → {"ok": true}
```

```javascript
fetch('http://10.221.208.1:8081/capture', { method: 'POST' })
  .then(r => r.json())
  .then(d => console.log(d.ok)); // true on success
```

## Reading depth values (JavaScript)

```javascript
function handleBundle(d) {
    // Decode depth PNG to pixel values
    const img = new Image();
    img.onload = () => {
        const canvas = document.createElement('canvas');
        canvas.width = 640; canvas.height = 480;
        const ctx = canvas.getContext('2d');
        ctx.drawImage(img, 0, 0);
        const pixels = ctx.getImageData(0, 0, 640, 480).data;
        // Each pixel: R=high byte, G=low byte of uint16 mm value
        // pixel[i*4+0] << 8 | pixel[i*4+1] = depth in mm
        const depthMM = pixels[0*4] << 8 | pixels[0*4+1]; // top-left pixel
        console.log('top-left depth:', depthMM, 'mm');
    };
    img.src = 'data:image/png;base64,' + d.depth_b64;

    // Show RGB
    document.getElementById('preview').src =
        'data:image/jpeg;base64,' + d.rgb_b64;
}
```

## Reading the live haptic grid (without capture)

The 21-cell haptic grid is available at all times via HTTP:

```bash
curl http://10.221.208.1:8081/grid
```

```json
{
  "grid":    [0, 0, 127, 255, 200, 0, 0, ...],
  "raw_mm":  [0, 0, 950, 320, 580, 0, 0, ...],
  "status":  "depth->haptic active ...",
  "usb_mode": "host"
}
```

- `grid[i]` — haptic intensity 0–255 for cell `i` (0=open, 255=close obstacle)
- `raw_mm[i]` — raw median distance in millimetres for cell `i`
- Cell index: `i = row * 7 + col` (row 0 = top, col 0 = wearer's left)

Poll this endpoint at any rate you like — the server updates it at ~30fps.

## Haptic grid layout

```
col:   0    1    2    3    4    5    6
row 0 [00] [01] [02] [03] [04] [05] [06]   top
row 1 [07] [08] [09] [10] [11] [12] [13]   middle
row 2 [14] [15] [16] [17] [18] [19] [20]   bottom
                                            (col 0 = wearer's LEFT)
```

## Python example

```python
import asyncio, websockets, json, base64

async def main():
    async with websockets.connect('ws://10.221.208.1:8083') as ws:
        async for msg in ws:
            d = json.loads(msg)
            rgb_bytes   = base64.b64decode(d['rgb_b64'])
            depth_bytes = base64.b64decode(d['depth_b64'])
            print(f"received: {len(rgb_bytes)//1024}KB rgb, "
                  f"{len(depth_bytes)//1024}KB depth, ts={d['ts']:.1f}")

asyncio.run(main())
```
