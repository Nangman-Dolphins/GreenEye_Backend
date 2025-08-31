# backend_app/inference.py

import torch
import torchvision.transforms as transforms
from torch.utils.data import DataLoader
from PIL import Image
import io
import os
import glob # 여러 fold 모델을 찾기 위해 추가

# ==============================================================================
# ⚙️ 1. 추론 설정 (CONFIGURATION)
# ==============================================================================
class InferenceConfig:
    """추론에 사용될 하이퍼파라미터 및 설정"""
    TILE_SIZE = 224
    STRIDE = 112
    INFERENCE_BATCH_SIZE = 8
    ENABLE_FIVECROP_TTA = True
    FIVECROP_BASE_SIZE = 256
    AGGREGATION_MODE = "topk_mean"
    TOP_K_TILES = 5

# ==============================================================================
# 2. 핵심 유틸리티 (CLASSES & FUNCTIONS)
# ==============================================================================
class TileDataset(torch.utils.data.Dataset):
    """이미지를 타일 단위로 자르는 데이터셋"""
    def __init__(self, image, tile_size, stride, transform, enable_fivecrop, fivecrop_base_size):
        self.image = image.convert("RGB")
        self.transform = transform
        self.tile_size = tile_size
        self.enable_fivecrop = enable_fivecrop
        self.fivecrop_base_size = fivecrop_base_size
        W, H = self.image.size
        xs = list(range(0, max(W - self.tile_size, 0) + 1, stride))
        ys = list(range(0, max(H - self.tile_size, 0) + 1, stride))
        if not xs or xs[-1] != max(W - self.tile_size, 0): xs.append(max(W - self.tile_size, 0))
        if not ys or ys[-1] != max(H - self.tile_size, 0): ys.append(max(H - self.tile_size, 0))
        self.coords = [(x, y) for y in sorted(set(ys)) for x in sorted(set(xs))]
        if not self.coords:
            self.coords = [(0, 0)]
            self.image = self.image.resize((self.tile_size, self.tile_size))

    def __len__(self): return len(self.coords)
    def __getitem__(self, idx):
        x, y = self.coords[idx]
        tile_pil = self.image.crop((x, y, x + self.tile_size, y + self.tile_size))
        if self.enable_fivecrop:
            resized_tile = tile_pil.resize((self.fivecrop_base_size, self.fivecrop_base_size), Image.Resampling.BICUBIC)
            five_crops = transforms.FiveCrop(self.tile_size)(resized_tile)
            return torch.stack([self.transform(crop) for crop in five_crops])
        return self.transform(tile_pil)

def aggregate_predictions(logits_TxC, mode, topk):
    """여러 타일의 예측 결과를 하나의 확률 벡터로 집계"""
    probs_TxC = torch.softmax(logits_TxC, dim=1)
    if mode == "mean":
        return probs_TxC.mean(dim=0)
    elif mode == "max":
        agg_probs, _ = probs_TxC.max(dim=0)
        return agg_probs
    elif mode == "topk_mean":
        k = min(topk, max(1, probs_TxC.size(0)))
        top1_vals, _ = probs_TxC.max(dim=1)
        indices = torch.topk(top1_vals, k=k).indices
        return probs_TxC[indices].mean(dim=0)
    else:
        raise ValueError(f"알 수 없는 집계 모드: {mode}")

# ==============================================================================
# 3. 추론기 클래스
# ==============================================================================
class PlantDiseaseClassifier:
    """단일 모델을 로드하고 고급 추론을 수행하는 클래스"""
    def __init__(self, model_path, class_labels):
        self.device = torch.device("cpu")
        self.class_labels = class_labels
        self.model = self._load_model(model_path)
        self.transform = self._get_transform()
        self.cfg = InferenceConfig()

    def _load_model(self, path):
        try:
            model = torch.jit.load(path, map_location=self.device)
            model.to(self.device).eval()
            print(f"✅ TorchScript 모델 로드 성공: {path}")
            return model
        except Exception as e:
            print(f"❌ TorchScript 모델 로드 실패: {path}, 에러: {e}")
            return None

    def _get_transform(self):
        return transforms.Compose([
            transforms.ToTensor(),
            transforms.Normalize(mean=[0.485, 0.456, 0.406], std=[0.229, 0.224, 0.225])
        ])

    def predict_probabilities(self, image_bytes):
        """이미지를 받아 최종 확률 벡터(agg_probs)를 반환"""
        if not self.model: return None
        try:
            image = Image.open(io.BytesIO(image_bytes))
            tile_dataset = TileDataset(image, self.cfg.TILE_SIZE, self.cfg.STRIDE, self.transform,
                                       self.cfg.ENABLE_FIVECROP_TTA, self.cfg.FIVECROP_BASE_SIZE)
            if len(tile_dataset) == 0: return None
            tile_loader = DataLoader(tile_dataset, batch_size=self.cfg.INFERENCE_BATCH_SIZE, num_workers=0)
            all_logits = []
            with torch.no_grad():
                for batch in tile_loader:
                    if self.cfg.ENABLE_FIVECROP_TTA:
                        bs, n_crops, c, h, w = batch.shape
                        batch = batch.view(-1, c, h, w)
                    batch = batch.to(self.device)
                    logits = (self.model(batch) + self.model(torch.flip(batch, dims=[3]))) / 2.0
                    if self.cfg.ENABLE_FIVECROP_TTA:
                        logits = logits.view(bs, n_crops, -1).mean(dim=1)
                    all_logits.append(logits.cpu())
            all_logits_tensor = torch.cat(all_logits, dim=0)
            return aggregate_predictions(all_logits_tensor, self.cfg.AGGREGATION_MODE, self.cfg.TOP_K_TILES)
        except Exception:
            return None

class EnsembleClassifier:
    """여러 fold 모델의 결과를 앙상블하는 클래스"""
    def __init__(self, classifiers, class_labels):
        self.classifiers = classifiers
        self.class_labels = class_labels

    def predict(self, image_bytes):
        all_probs = []
        for classifier in self.classifiers:
            probs = classifier.predict_probabilities(image_bytes)
            if probs is not None:
                all_probs.append(probs)

        if not all_probs:
            return {"error": "모든 fold 모델에서 추론에 실패했습니다."}

        # Soft Voting: 모든 모델의 확률 벡터를 평균
        mean_probs = torch.stack(all_probs).mean(dim=0)
        
        final_confidence, final_class_idx = torch.max(mean_probs, dim=0)
        predicted_label = self.class_labels[final_class_idx.item()]
        
        return {
            "predicted_label": predicted_label,
            "confidence": f"{final_confidence.item():.2%}",
            "class_index": final_class_idx.item(),
            "ensembled_folds": len(all_probs)
        }

# ==============================================================================
# 4. 다중 모델 관리자 (앙상블 기능 추가)
# ==============================================================================
class ModelManager:
    """식물 종류에 따라 여러 fold 모델을 앙상블하여 관리합니다."""
    def __init__(self, base_model_dir):
        self.base_dir = base_model_dir
        self.ensemble_classifiers = {} # 앙상블 모델을 캐싱

    def get_classifier(self, plant_type: str):
        effective_plant_type = plant_type if plant_type else "default"
        
        if effective_plant_type in self.ensemble_classifiers:
            return self.ensemble_classifiers[effective_plant_type]

        print(f"'{effective_plant_type}' 앙상블 모델을 로드합니다...")
        
        # 1. 클래스 레이블 로드
        labels_path = os.path.join(self.base_dir, f"{effective_plant_type}_classes.txt")
        try:
            with open(labels_path, 'r', encoding='utf-8') as f:
                class_labels = [line.strip() for line in f if line.strip()]
        except Exception:
            print(f"❌ 에러: '{labels_path}' 클래스 파일을 찾을 수 없습니다.")
            return None

        # 2. 모든 fold 모델 파일 찾기
        model_pattern = os.path.join(self.base_dir, f"{effective_plant_type}_model_fold*.pt")
        model_paths = glob.glob(model_pattern)

        if not model_paths and effective_plant_type != "default":
            print(f"⚠️ 경고: '{effective_plant_type}'의 fold 모델을 찾을 수 없습니다. 기본 모델을 사용합니다.")
            return self.get_classifier("default")
        
        if not model_paths:
            print(f"❌ 에러: '{effective_plant_type}' 모델 파일을 찾을 수 없습니다.")
            return None

        # 3. 각 fold 모델에 대한 분류기 생성
        individual_classifiers = []
        for model_path in sorted(model_paths):
            classifier = PlantDiseaseClassifier(model_path, class_labels)
            if classifier.model: # 모델 로딩 성공 시에만 추가
                individual_classifiers.append(classifier)

        if not individual_classifiers:
            print(f"❌ 에러: '{effective_plant_type}'의 모델을 하나도 로드하지 못했습니다.")
            return None

        # 4. 앙상블 분류기 생성 및 캐싱
        ensemble_classifier = EnsembleClassifier(individual_classifiers, class_labels)
        self.ensemble_classifiers[effective_plant_type] = ensemble_classifier
        return ensemble_classifier

    def predict(self, image_bytes, plant_type: str):
        classifier = self.get_classifier(plant_type)
        if not classifier:
            final_plant_type = plant_type if plant_type else "default"
            return {"error": f"'{final_plant_type}'에 해당하는 모델 또는 기본 모델을 찾을 수 없습니다."}
        
        return classifier.predict(image_bytes)

# --- 모델 매니저 인스턴스 생성 ---
base_dir = os.path.dirname(os.path.abspath(__file__))
MODEL_FOLDER_PATH = os.path.join(base_dir, "ml_models")
model_manager = ModelManager(MODEL_FOLDER_PATH)