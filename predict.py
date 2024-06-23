import json
import os
import shutil
import subprocess
import time
from typing import Any, Callable, Dict, List, Optional, Tuple, Union
from weights import WeightsDownloadCache
from tqdm import tqdm
from pathlib import Path
import threading

import numpy as np
import torch
import cv2
from cog import BasePredictor, Input, Path
from PIL import Image
from diffusers import (
    DDIMScheduler,
    DPMSolverMultistepScheduler,
    EulerAncestralDiscreteScheduler,
    EulerDiscreteScheduler,
    HeunDiscreteScheduler,
    PNDMScheduler,
    StableDiffusionXLControlNetPipeline,
    StableDiffusionXLControlNetImg2ImgPipeline,
    ControlNetModel
)
from diffusers.models.attention_processor import LoRAAttnProcessor2_0

from transformers import (
    BlipForConditionalGeneration,
    BlipProcessor,
)

from diffusers.utils import load_image
from safetensors.torch import load_file

from dataset_and_utils import TokenEmbeddingsHandler

CONTROL_CACHE = "control-cache"
SDXL_MODEL_CACHE = "./sdxl-cache"
FEATURE_EXTRACTOR = "./feature-extractor"
SDXL_URL = "https://weights.replicate.delivery/default/sdxl/sdxl-vae-upcast-fix.tar"
WEIGHT_CACHE_DIR = "./weights-cache"

# model is fixed to Salesforce/blip-image-captioning-large
BLIP_URL = "https://weights.replicate.delivery/default/blip_large/blip_large.tar"
BLIP_PROCESSOR_URL = "https://weights.replicate.delivery/default/blip_processor/blip_processor.tar"
BLIP_PATH = "./blip-cache/blip_large"
BLIP_PROCESSOR_PATH = "./blip-cache/blip_processor"


class KarrasDPM:
    def from_config(config):
        return DPMSolverMultistepScheduler.from_config(config, use_karras_sigmas=True)


SCHEDULERS = {
    "DDIM": DDIMScheduler,
    "DPMSolverMultistep": DPMSolverMultistepScheduler,
    "HeunDiscrete": HeunDiscreteScheduler,
    "KarrasDPM": KarrasDPM,
    "K_EULER_ANCESTRAL": EulerAncestralDiscreteScheduler,
    "K_EULER": EulerDiscreteScheduler,
    "PNDM": PNDMScheduler,
}


def download_weight(url: str, dest: str):
    start = time.time()
    print("downloading url: ", url)
    print("downloading to: ", dest)
    subprocess.check_call(["pget", "-x", url, dest], close_fds=False)
    print("downloading took: ", time.time() - start)


def download_weights(tasks: List[Tuple[str, str]]):
    # スレッドを作成してコマンドを実行
    threads = []
    for url, dest in tasks:
        thread = threading.Thread(target=download_weight, args=(url, dest))
        thread.start()
        threads.append(thread)

    # 全てのスレッドが終了するのを待つ
    for thread in threads:
        thread.join()


class Predictor(BasePredictor):

    def __init__(self):
        self.device = "cuda" if torch.cuda.is_available() else "cpu"
        self.no_half = False if torch.cuda.is_available() else True

    def load_trained_weights(self, weights, pipe):
        print("loading custom weights")
        from no_init import no_init_or_tensor

        # weights can be a URLPath, which behaves in unexpected ways
        weights = str(weights)
        # if self.tuned_weights == weights:
        # print("skipping loading .. weights already loaded")
        # return

        self.tuned_weights = weights

        local_weights_cache = self.weights_cache.ensure(weights)

        # load UNET
        print("Loading fine-tuned model")
        self.is_lora = False

        maybe_unet_path = os.path.join(local_weights_cache, "unet.safetensors")
        if not os.path.exists(maybe_unet_path):
            print("Does not have Unet. assume we are using LoRA")
            self.is_lora = True

        if not self.is_lora:
            print("Loading Unet")

            new_unet_params = load_file(
                os.path.join(local_weights_cache, "unet.safetensors")
            )
            # this should return _IncompatibleKeys(missing_keys=[...], unexpected_keys=[])
            pipe.unet.load_state_dict(new_unet_params, strict=False)

        else:
            print("Loading Unet LoRA")

            unet = pipe.unet

            tensors = load_file(os.path.join(
                local_weights_cache, "lora.safetensors"))

            unet_lora_attn_procs = {}
            name_rank_map = {}
            for tk, tv in tensors.items():
                # up is N, d
                if tk.endswith("up.weight"):
                    proc_name = ".".join(tk.split(".")[:-3])
                    r = tv.shape[1]
                    name_rank_map[proc_name] = r

            for name, attn_processor in unet.attn_processors.items():
                cross_attention_dim = (
                    None
                    if name.endswith("attn1.processor")
                    else unet.config.cross_attention_dim
                )
                if name.startswith("mid_block"):
                    hidden_size = unet.config.block_out_channels[-1]
                elif name.startswith("up_blocks"):
                    block_id = int(name[len("up_blocks.")])
                    hidden_size = list(reversed(unet.config.block_out_channels))[
                        block_id
                    ]
                elif name.startswith("down_blocks"):
                    block_id = int(name[len("down_blocks.")])
                    hidden_size = unet.config.block_out_channels[block_id]
                with no_init_or_tensor():
                    module = LoRAAttnProcessor2_0(
                        hidden_size=hidden_size,
                        cross_attention_dim=cross_attention_dim,
                        rank=name_rank_map[name],
                    )
                unet_lora_attn_procs[name] = module.to(
                    self.device, non_blocking=True)

            unet.set_attn_processor(unet_lora_attn_procs)
            unet.load_state_dict(tensors, strict=False)

        # load text
        handler = TokenEmbeddingsHandler(
            [pipe.text_encoder, pipe.text_encoder_2], [
                pipe.tokenizer, pipe.tokenizer_2]
        )
        handler.load_embeddings(os.path.join(
            local_weights_cache, "embeddings.pti"))

        # load params
        with open(os.path.join(local_weights_cache, "special_params.json"), "r") as f:
            params = json.load(f)
        self.token_map = params

        self.tuned_model = True

    def setup(self, weights: Optional[Path] = None):
        """Load the model into memory to make running multiple predictions efficient"""
        start = time.time()
        self.tuned_model = False
        self.tuned_weights = None
        if str(weights) == "weights":
            weights = None

        self.weights_cache = WeightsDownloadCache(base_dir=WEIGHT_CACHE_DIR)

        download_tasks = []
        if not os.path.exists(BLIP_PROCESSOR_PATH):
            download_tasks.append((BLIP_PROCESSOR_URL, BLIP_PROCESSOR_PATH))
        if not os.path.exists(BLIP_PATH):
            download_tasks.append((BLIP_URL, BLIP_PATH))
        if not os.path.exists(SDXL_MODEL_CACHE):
            download_tasks.append((SDXL_URL, SDXL_MODEL_CACHE))

        if download_tasks:
            download_weights(download_tasks)

        self.blip_processor = BlipProcessor.from_pretrained(
            BLIP_PROCESSOR_PATH)
        self.blip_model = BlipForConditionalGeneration.from_pretrained(
            BLIP_PATH).to(self.device)

        controlnet = ControlNetModel.from_pretrained(
            CONTROL_CACHE,
            torch_dtype=torch.float32 if self.no_half else torch.float16,
        )

        print("Loading SDXL Controlnet pipeline...")
        self.control_text2img_pipe = StableDiffusionXLControlNetPipeline.from_pretrained(
            SDXL_MODEL_CACHE,
            controlnet=controlnet,
            torch_dtype=torch.float32 if self.no_half else torch.float16,
            use_safetensors=True,
            variant="fp16",
        )
        self.control_text2img_pipe.to(self.device)

        self.control_img2img_pipe = StableDiffusionXLControlNetImg2ImgPipeline(
            vae=self.control_text2img_pipe.vae,
            text_encoder=self.control_text2img_pipe.text_encoder,
            text_encoder_2=self.control_text2img_pipe.text_encoder_2,
            tokenizer=self.control_text2img_pipe.tokenizer,
            tokenizer_2=self.control_text2img_pipe.tokenizer_2,
            unet=self.control_text2img_pipe.unet,
            scheduler=self.control_text2img_pipe.scheduler,
            controlnet=controlnet,
        )
        self.control_img2img_pipe.to(self.device)

        self.is_lora = False
        if weights or os.path.exists("./trained-model"):
            self.load_trained_weights(weights, self.control_text2img_pipe)

        print("setup took: ", time.time() - start)

    def load_image(self, path):
        shutil.copyfile(path, "/tmp/image.png")
        return load_image("/tmp/image.png").convert("RGB")

    def resize_image(self, image):
        image_width, image_height = image.size
        print("Original width:"+str(image_width)+", height:"+str(image_height))
        new_width, new_height = self.resize_to_allowed_dimensions(
            image_width, image_height)
        print("new_width:"+str(new_width)+", new_height:"+str(new_height))
        image = image.resize((new_width, new_height))
        return image, new_width, new_height

    def resize_to_allowed_dimensions(self, width, height):
        """
        Function re-used from Lucataco's implementation of SDXL-Controlnet for Replicate
        """
        # List of SDXL dimensions
        allowed_dimensions = [
            (512, 2048), (512, 1984), (512, 1920), (512, 1856),
            (576, 1792), (576, 1728), (576, 1664), (640, 1600),
            (640, 1536), (704, 1472), (704, 1408), (704, 1344),
            (768, 1344), (768, 1280), (832, 1216), (832, 1152),
            (896, 1152), (896, 1088), (960, 1088), (960, 1024),
            (1024, 1024), (1024, 960), (1088, 960), (1088, 896),
            (1152, 896), (1152, 832), (1216, 832), (1280, 768),
            (1344, 768), (1408, 704), (1472, 704), (1536, 640),
            (1600, 640), (1664, 576), (1728, 576), (1792, 576),
            (1856, 512), (1920, 512), (1984, 512), (2048, 512)
        ]
        # Calculate the aspect ratio
        aspect_ratio = width / height
        print(f"Aspect Ratio: {aspect_ratio:.2f}")
        # Find the closest allowed dimensions that maintain the aspect ratio
        closest_dimensions = min(
            allowed_dimensions,
            key=lambda dim: abs(dim[0] / dim[1] - aspect_ratio)
        )
        return closest_dimensions

    def image2canny(self, image):
        image = np.array(image)
        image = cv2.Canny(image, 100, 200)
        image = image[:, :, None]
        image = np.concatenate([image, image, image], axis=2)
        return Image.fromarray(image)

    @torch.no_grad()
    def blip_captioning_dataset(
        self,
        images: List[Image.Image],
        text: Optional[str] = None,
        device=torch.device("cuda" if torch.cuda.is_available() else "cpu"),
        substitution_tokens: Optional[List[str]] = None,
        **kwargs,
    ) -> List[str]:
        """
        Returns a list of captions for the given images
        """
        captions = []
        if text is not None:
            text = text.strip()
            print(f"Input captioning text: {text}")
        for image in tqdm(images):
            inputs = self.blip_processor(
                image, return_tensors="pt").to(self.device)
            out = self.blip_model.generate(
                **inputs, max_length=150, do_sample=True, top_k=50, temperature=0.7
            )
            caption = self.blip_processor.decode(
                out[0], skip_special_tokens=True)

            # BLIP 2 lowercases all caps tokens. This should properly replace them w/o messing up subwords. I'm sure there's a better way to do this.
            if substitution_tokens is not None:
                for token in substitution_tokens:
                    print(token)
                    sub_cap = " " + caption + " "
                    print(sub_cap)
                    sub_cap = sub_cap.replace(
                        " " + token.lower() + " ", " " + token + " ")
                    caption = sub_cap.strip()

            if text is not None:
                captions.append(text + " " + caption)
        print("Generated captions", captions)
        return captions

    @torch.inference_mode()
    def predict(
        self,
        prompt: str = Input(
            description="Input prompt",
            default="An astronaut riding a rainbow unicorn",
        ),
        image: Path = Input(
            description="Input image for img2img or inpaint mode",
            default=None,
        ),
        img2img: bool = Input(
            description="Use img2img pipeline, it will use the image input both as the control image and the base image.",
            default=False
        ),
        auto_generate_caption: bool = Input(
            description="Use BLIP to generate captions for the input images",
            default=False
        ),
        condition_scale: float = Input(
            description="The bigger this number is, the more ControlNet interferes",
            default=1.1,
            ge=0.0,
            le=2.0,
        ),
        strength: float = Input(
            description="When img2img is active, the denoising strength. 1 means total destruction of the input image.",
            default=0.8,
            ge=0.0,
            le=1.0,
        ),
        negative_prompt: str = Input(
            description="Input Negative Prompt",
            default="",
        ),
        num_inference_steps: int = Input(
            description="Number of denoising steps", ge=1, le=500, default=30
        ),
        num_outputs: int = Input(
            description="Number of images to output",
            ge=1,
            le=4,
            default=1,
        ),
        scheduler: str = Input(
            description="scheduler",
            choices=SCHEDULERS.keys(),
            default="K_EULER",
        ),
        guidance_scale: float = Input(
            description="Scale for classifier-free guidance", ge=1, le=50, default=7.5
        ),
        seed: int = Input(
            description="Random seed. Leave blank to randomize the seed", default=None
        ),
        lora_scale: float = Input(
            description="LoRA additive scale. Only applicable on trained models.",
            ge=0.0,
            le=1.0,
            default=0.95,
        ),
        lora_weights: str = Input(
            description="Replicate LoRA weights to use. Leave blank to use the default weights.",
            default=None,
        ),
    ) -> List[Path]:
        """Run a single prediction on the model"""
        if seed is None:
            seed = int.from_bytes(os.urandom(2), "big")
        print(f"Using seed: {seed}")

        if lora_weights:
            self.load_trained_weights(lora_weights, self.control_text2img_pipe)

        # OOMs can leave vae in bad state
        if self.control_text2img_pipe.vae.dtype == torch.float32:
            self.control_text2img_pipe.vae.to(
                dtype=torch.float32 if self.no_half else torch.float16)

        sdxl_kwargs = {}
        if self.tuned_model:
            # consistency with fine-tuning API
            for k, v in self.token_map.items():
                prompt = prompt.replace(k, v)
        image = self.load_image(image)
        image, width, height = self.resize_image(image)

        if auto_generate_caption:
            print("auto_generate_caption mode")
            captions = self.blip_captioning_dataset([image], prompt)
            prompt = captions[0]
        print(f"Prompt: {prompt}")

        if (img2img):
            print("img2img mode")
            sdxl_kwargs["image"] = image
            sdxl_kwargs["control_image"] = self.image2canny(image)
            sdxl_kwargs["strength"] = strength
            sdxl_kwargs["controlnet_conditioning_scale"] = condition_scale
            sdxl_kwargs["width"] = width
            sdxl_kwargs["height"] = height
            pipe = self.control_img2img_pipe

        else:
            print("text2img mode")
            sdxl_kwargs["image"] = self.image2canny(image)
            sdxl_kwargs["controlnet_conditioning_scale"] = condition_scale
            sdxl_kwargs["width"] = width
            sdxl_kwargs["height"] = height
            pipe = self.control_text2img_pipe

        pipe.scheduler = SCHEDULERS[scheduler].from_config(
            pipe.scheduler.config)
        generator = torch.Generator(self.device).manual_seed(seed)

        common_args = {
            "prompt": [prompt] * num_outputs,
            "negative_prompt": [negative_prompt] * num_outputs,
            "guidance_scale": guidance_scale,
            "generator": generator,
            "num_inference_steps": num_inference_steps,
        }

        if self.is_lora:
            sdxl_kwargs["cross_attention_kwargs"] = {"scale": lora_scale}

        output = pipe(**common_args, **sdxl_kwargs)

        output_paths = []
        for i, image in enumerate(output.images):
            output_path = f"/tmp/out-{i}.png"
            image.save(output_path)
            output_paths.append(Path(output_path))

        return output_paths
