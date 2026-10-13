"""Tell cameras apart by their picture, for HW3 cars.

HW3 records four cameras at the same resolution with identical encoder settings, so nothing in
the file says which camera it is; the app relies on the order the car writes a minute's files
in (front, left repeater, right repeater, back). That order is occasionally different (seen on
Sentry clips). The back camera is a fisheye lens with dark, rounded corners, which makes it stand
out: on 162 decoded HW3 minutes, the clip with the darkest corners was the back camera every
time, typically at 65 % of the next-darkest camera's corner brightness. The front and the two
repeaters can't be told apart this way.
"""

W, H = 160, 120
CORNER = 12
SAMPLE_TIMES = (10, 30, 50)


def corner_darkness(path):
    """Mean brightness of the four image corners relative to the centre (averaged over a few
    keyframes), or None if the clip can't be decoded here (PyAV missing, broken clip)."""
    try:
        import av
    except ImportError:
        return None
    ratios = []
    try:
        with av.open(path) as container:
            stream = container.streams.video[0]
            stream.codec_context.skip_frame = 'NONKEY'
            duration = float(container.duration or 0) / 1e6
            for t in SAMPLE_TIMES:
                if duration and t > duration - 1:
                    t = duration / 2
                container.seek(int(t * 1e6))
                frame = next(container.decode(stream), None)
                if frame is None:
                    continue
                plane = frame.reformat(width=W, height=H, format='gray').planes[0]
                data, stride = bytes(plane), plane.line_size

                def box(x0, y0, x1, y1):
                    return sum(sum(data[y * stride + x0:y * stride + x1]) for y in range(y0, y1)) / ((x1 - x0) * (y1 - y0))
                k = CORNER
                corners = (box(0, 0, k, k) + box(W - k, 0, W, k) + box(0, H - k, k, H) + box(W - k, H - k, W, H)) / 4
                ratios.append(corners / (box(50, 35, 110, 85) + 1))
    except Exception:
        return None
    return sum(ratios) / len(ratios) if ratios else None


def find_back_camera(paths, clear_ratio=0.8):
    """Of a minute's four clips, the index of the back camera if the picture makes it clear
    (its corners at most clear_ratio as bright as any other clip's), else None."""
    values = [corner_darkness(p) for p in paths]
    if any(v is None for v in values):
        return None
    order = sorted(range(len(values)), key=lambda i: values[i])
    darkest, second = values[order[0]], values[order[1]]
    return order[0] if second > 0 and darkest / second <= clear_ratio else None
