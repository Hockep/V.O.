import cv2
import torch
import os
from pathlib import Path
import pickle
from torch import nn
import shutil

def simple_nms(scores, nms_radius: int):
  
    # Перевіряємо, що радіус NMS не від'ємний
    assert(nms_radius >= 0)

    # Функція для виконання max pooling
    def max_pool(x):
        return torch.nn.functional.max_pool2d(
            x, kernel_size=nms_radius*2+1, stride=1, padding=nms_radius)

    zeros = torch.zeros_like(scores)
    max_mask = scores == max_pool(scores)

    for _ in range(2):
        supp_mask = max_pool(max_mask.float()) > 0
        supp_scores = torch.where(supp_mask, zeros, scores)
        new_max_mask = supp_scores == max_pool(supp_scores)
        max_mask = max_mask | (new_max_mask & (~supp_mask))
    return torch.where(max_mask, scores, zeros)

def remove_borders(keypoints, scores, border: int, height: int, width: int):
    # Маска для видалення ключових точок біля меж зображення
    mask_h = (keypoints[:, 0] >= border) & (keypoints[:, 0] < (height - border))
    mask_w = (keypoints[:, 1] >= border) & (keypoints[:, 1] < (width - border))
    mask = mask_h & mask_w
    return keypoints[mask], scores[mask]


def top_k_keypoints(keypoints, scores, k: int):
    # Якщо кількість ключових точок менша за k, повертаємо всі точки
    if k >= len(keypoints):
        return keypoints, scores
    # Вибираємо k ключових точок з найвищими оцінками
    scores, indices = torch.topk(scores, k, dim=0)
    return keypoints[indices], scores

def sample_descriptors(keypoints, descriptors, s: int = 8):
    # Нормалізуємо координати ключових точок
    b, c, h, w = descriptors.shape
    keypoints = keypoints - s / 2 + 0.5
    keypoints /= torch.tensor([(w*s - s/2 - 0.5), (h*s - s/2 - 0.5)],
                              ).to(keypoints)[None]
    keypoints = keypoints*2 - 1  # нормалізуємо до (-1, 1)

    # Вибираємо дескриптори, які відповідають ключовим точкам
    args = {'align_corners': True} if int(torch.__version__[2]) > 2 else {}
    descriptors = torch.nn.functional.grid_sample(
    descriptors, keypoints.view(b, 1, -1, 2), mode='bilinear', **args)
    descriptors = torch.nn.functional.normalize(
    descriptors.reshape(b, c, -1), p=2, dim=1)
    return descriptors

class SuperPoint(nn.Module):
    default_config = {
        'descriptor_dim': 256,
        'nms_radius': 4,
        'keypoint_threshold': 0.005,
        'max_keypoints': -1,
        'remove_borders': 4,
    }

    # Конструктор класу
    def __init__(self, config):
        # Викликаємо конструктор батьківського класу
        super().__init__()
        self.config = {**self.default_config, **config}

        # Створюємо шари нейронної мережі
        self.relu = nn.ReLU(inplace=True)
        self.pool = nn.MaxPool2d(kernel_size=2, stride=2)
        c1, c2, c3, c4, c5 = 64, 64, 128, 128, 256

        # Шари для обчислення ключових точок
        self.conv1a = nn.Conv2d(1, c1, kernel_size=3, stride=1, padding=1)
        self.conv1b = nn.Conv2d(c1, c1, kernel_size=3, stride=1, padding=1)
        self.conv2a = nn.Conv2d(c1, c2, kernel_size=3, stride=1, padding=1)
        self.conv2b = nn.Conv2d(c2, c2, kernel_size=3, stride=1, padding=1)
        self.conv3a = nn.Conv2d(c2, c3, kernel_size=3, stride=1, padding=1)
        self.conv3b = nn.Conv2d(c3, c3, kernel_size=3, stride=1, padding=1)
        self.conv4a = nn.Conv2d(c3, c4, kernel_size=3, stride=1, padding=1)
        self.conv4b = nn.Conv2d(c4, c4, kernel_size=3, stride=1, padding=1)
        self.convPa = nn.Conv2d(c4, c5, kernel_size=3, stride=1, padding=1)
        self.convPb = nn.Conv2d(c5, 65, kernel_size=1, stride=1, padding=0)

        # Шари для обчислення дескрипторів
        self.convDa = nn.Conv2d(c4, c5, kernel_size=3, stride=1, padding=1)
        self.convDb = nn.Conv2d(
            c5, self.config['descriptor_dim'],
            kernel_size=1, stride=1, padding=0)

        # Завантажуємо ваги моделі
        path = Path(__file__).parent / 'weights/superpoint_v1.pth'
        self.load_state_dict(torch.load(str(path), weights_only = True))

        # Перевіряємо, що параметри моделі задовільняють обмеження
        mk = self.config['max_keypoints']
        if mk == 0 or mk < -1:
            raise ValueError('\"max_keypoints\" must be positive or \"-1\"')
        
    # Метод для визначення ключових точок та дескрипторів
    def forward(self, data):
        # Обчислюємо ключові точки
        x = self.relu(self.conv1a(data['image']))
        x = self.relu(self.conv1b(x))
        x = self.pool(x)
        x = self.relu(self.conv2a(x))
        x = self.relu(self.conv2b(x))
        x = self.pool(x)
        x = self.relu(self.conv3a(x))
        x = self.relu(self.conv3b(x))
        x = self.pool(x)
        x = self.relu(self.conv4a(x))
        x = self.relu(self.conv4b(x))

        # Оцінюємо ключові точки
        cPa = self.relu(self.convPa(x))
        scores = self.convPb(cPa)
        scores = torch.nn.functional.softmax(scores, 1)[:, :-1]
        b, _, h, w = scores.shape
        scores = scores.permute(0, 2, 3, 1).reshape(b, h, w, 8, 8)
        scores = scores.permute(0, 1, 3, 2, 4).reshape(b, h*8, w*8)
        scores = simple_nms(scores, 4)  # nms_radius

        # Визначаємо ключові точки з оцінкою більше порогового значення
        keypoints = [
            torch.nonzero(s > 0.005)  # keypoint_threshold
            for s in scores]
        scores = [s[tuple(k.t())] for s, k in zip(scores, keypoints)]

        # Видаляємо ключові точки біля меж зображення
        keypoints, scores = list(zip(*[
            remove_borders(k, s, 4, h*8, w*8)
            for k, s in zip(keypoints, scores)]))

        # Вибираємо кращі ключові точки
        keypoints, scores = list(zip(*[
            top_k_keypoints(k, s, 1024) 
            for k, s in zip(keypoints, scores)]))
        keypoints = [torch.flip(k, [1]).float() for k in keypoints]

        # Обчислюємо дескриптори
        cDa = self.relu(self.convDa(x))
        descriptors = self.convDb(cDa)
        descriptors = torch.nn.functional.normalize(descriptors, p=2, dim=1)

        # Вибираємо дескриптори, які відповідають ключовим точкам
        descriptors = [sample_descriptors(k[None], d[None], 8)[0]
                       for k, d in zip(keypoints, descriptors)]

        return {
            'keypoints': keypoints,
            'scores': scores,
            'descriptors': descriptors,
        }

def img2superpoint(input_dir, output_path):
    # Вимикаємо автоматичне обчислення градієнтів
    torch.set_grad_enabled(False)
    
    # Завантажуємо модель SuperPoint
    superpoint = SuperPoint({}).eval().to('cpu')

    # Завантажуємо всі зображення з вказаної директорії
    input_path = Path(input_dir)
    all_images_name = os.listdir(input_path)
    all_images_name = [image_name for image_name in all_images_name if image_name.endswith('.jpg')
                       or image_name.endswith('.png') or image_name.endswith('jpeg')]

    # Створюємо вихідну директорію
    if not os.path.exists(output_path):
        os.makedirs(output_path)
    else:
        # Якщо директорія існує видаляємо вміст вихідної директорії
        for item in os.listdir(output_path):
            item_path = os.path.join(output_path, item)
            if os.path.isfile(item_path):
                os.remove(item_path)  # Видаляє файл
            elif os.path.isdir(item_path):
                shutil.rmtree(item_path)  # Видаляє каталог з усім вмістом

    # Обробляємо кожне зображення директорії
    for i, image_name in enumerate(all_images_name):
        stem_image_name = Path(image_name).stem

        # Завантажуємо зображення та перетворюємо його у масив
        image = cv2.imread(str(input_path / image_name), cv2.IMREAD_GRAYSCALE)
        image = cv2.resize(image, (640, 480))

        # Видаляємо шум за допомогою медіанного фільтра
        image = image.astype('float32')
        
        # Відрегулювати яскравість та контрастність
        tensor_image = torch.from_numpy(image / 255.).float()[None, None].to('cpu')

        # Передаємо зображення у модель SuperPoint
        pred = superpoint({'image': tensor_image})
        
        # Зберігаємо результат у файл .pickle
        with open(f'{output_path}/{stem_image_name}.pickle', 'wb') as file:
            pickle.dump(pred, file)