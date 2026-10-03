# Step 1 — YOLO face detection + recognition on a 720p webcam

YOLO only *detects*. Identifying who it is needs a second step (embeddings). Plan: YOLO finds the face, `face_recognition` names it.

## 1. Install (laptop first, RPi later)
```bash
python -m venv .venv && source .venv/bin/activate
pip install ultralytics opencv-python face_recognition numpy
```
`yolov8n.pt` downloads automatically on first use.

## 2. Sanity check: pretrained YOLO on the webcam
```python
from ultralytics import YOLO
m = YOLO("yolov8n.pt")
m.predict(source=0, show=True, imgsz=640, classes=[0])  # 0 = person
```

## 3. Train a face detector
Get a face dataset in YOLO format (Roboflow Universe "face detection", or WIDER FACE converted). Put its `data.yaml` in `datasets/faces/`.
```bash
yolo detect train model=yolov8n.pt data=datasets/faces/data.yaml epochs=50 imgsz=640 batch=16
```
Best weights: `runs/detect/train/weights/best.pt`. Copy to `models/face.pt`.

Add your own webcam images to the dataset (200+ frames, varied light and angle) so it fits your 720p camera.

## 4. Enroll people
```python
import face_recognition, numpy as np, pathlib
img = face_recognition.load_image_file("me.jpg")
enc = face_recognition.face_encodings(img)[0]
pathlib.Path("known_faces").mkdir(exist_ok=True)
np.save("known_faces/imman.npy", enc)
```
Use 5-10 photos per person, averaged or saved as separate files.

## 5. Live test at 1280x720
```python
import cv2, glob, os, numpy as np, face_recognition
from ultralytics import YOLO

det = YOLO("models/face.pt")
names = [os.path.basename(f)[:-4] for f in glob.glob("known_faces/*.npy")]
known = [np.load(f) for f in glob.glob("known_faces/*.npy")]

cap = cv2.VideoCapture(0)
cap.set(cv2.CAP_PROP_FRAME_WIDTH, 1280)
cap.set(cv2.CAP_PROP_FRAME_HEIGHT, 720)

while True:
    ok, frame = cap.read()
    if not ok: break
    for b in det(frame, imgsz=640, verbose=False)[0].boxes:
        x1, y1, x2, y2 = map(int, b.xyxy[0])
        rgb = cv2.cvtColor(frame[y1:y2, x1:x2], cv2.COLOR_BGR2RGB)
        encs = face_recognition.face_encodings(rgb, [(0, x2-x1, y2-y1, 0)])
        label = "unknown"
        if encs and known:
            d = face_recognition.face_distance(known, encs[0])
            if d.min() < 0.5: label = names[d.argmin()]
        cv2.rectangle(frame, (x1, y1), (x2, y2), (0, 255, 0), 2)
        cv2.putText(frame, label, (x1, y1-8), 0, 0.8, (0, 255, 0), 2)
    cv2.imshow("shield", frame)
    if cv2.waitKey(1) == 27: break
```

## Pass criteria
- Your face labelled correctly in normal room light.
- A different person labelled `unknown`.
- Stable at 720p on the laptop. Then repeat on the RPi 4B.
