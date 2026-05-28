"""
Printer service for managing Brother QL printer operations.
"""

import os
import sys
import uuid
import structlog
import threading
import time
import socket
import io
from typing import Dict, Any, List, Optional, Tuple

from PIL import Image, ImageDraw, ImageFont, ImageOps
import qrcode
import fitz

from brother_ql.raster import BrotherQLRaster
from brother_ql.conversion import convert
from brother_ql.backends import backend_factory, guess_backend

# Import pysnmp for SNMP-based printer communication
try:
    from pysnmp.hlapi import (
        SnmpEngine, CommunityData, UdpTransportTarget,
        ContextData, ObjectType, ObjectIdentity, getCmd
    )
    SNMP_AVAILABLE = True
except ImportError:
    SNMP_AVAILABLE = False
    logger = structlog.get_logger()
    logger.warning("pysnmp not available, SNMP-based keep-alive will not work")

from src.services.settings_service import settings_service
from src.utils.exceptions import PrinterError, ImageProcessingError

logger = structlog.get_logger()


class PrinterService:
    """Service for managing Brother QL printer operations."""

    def __init__(self, upload_folder: Optional[str] = None):
        """
        Initialize the printer service.

        Args:
            upload_folder: Path to the upload folder. If None, uses the default path.
        """
        self.keep_alive_thread = None
        self.keep_alive_stop_event = threading.Event()

        if upload_folder is None:
            self.upload_folder = os.path.join(
                os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                "uploads"
            )
        else:
            self.upload_folder = upload_folder

        os.makedirs(self.upload_folder, exist_ok=True)

        self.font_path = "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"

        if not os.path.exists(self.font_path):
            try:
                import matplotlib.font_manager as fm
                self.font_path = fm.findfont(
                    fm.FontProperties(family='DejaVu Sans')
                )
                logger.info("Using font", font_path=self.font_path)
            except ImportError:
                logger.warning("Matplotlib not available, using default font")
                self.font_path = None

    def print_image(self, image_path: str, settings: Dict[str, Any]) -> Dict[str, Any]:
        """
        Print an image or PDF on a label.
        """

        try:
            job_id = f"image_{uuid.uuid4().hex[:8]}"

            logger.info(
                "Processing image print request",
                job_id=job_id,
                image_path=image_path
            )

            #
            # PDF SUPPORT
            #
            if image_path.lower().endswith(".pdf"):
                logger.info(
                    "PDF detected, converting before print",
                    job_id=job_id,
                    image_path=image_path
                )

                resized_path = self._convert_pdf_to_image(image_path)

            else:
                resized_path = self._resize_image(image_path)

            logger.info(
                "Image prepared",
                job_id=job_id,
                resized_path=resized_path
            )

            rotate = settings.get("rotate", 0)

            if rotate != 0:
                resized_path = self._apply_rotation(
                    resized_path,
                    rotate
                )

                logger.info(
                    "Rotation applied",
                    job_id=job_id,
                    rotate=rotate
                )

            self._send_to_printer(resized_path, settings)

            logger.info(
                "Print job completed successfully",
                job_id=job_id
            )

            return {
                "success": True,
                "job_id": job_id,
                "message": "Image printed successfully"
            }

        except Exception as e:
            logger.error(
                "Error printing image",
                error=str(e),
                exc_info=True
            )

            raise PrinterError(f"Error printing image: {str(e)}")

    def _resize_image(self, image_path: str) -> str:
        """
        Resize an image to fit the label width.
        """

        try:
            max_width = 696

            with Image.open(image_path) as img:

                #
                # Convert to monochrome for better thermal printing
                #
                img = img.convert("1")

                aspect_ratio = img.height / img.width
                new_height = int(max_width * aspect_ratio)

                img = img.resize(
                    (max_width, new_height),
                    Image.Resampling.LANCZOS
                )

                filename = os.path.basename(image_path)

                resized_path = os.path.join(
                    self.upload_folder,
                    f"resized_{filename}"
                )

                img.save(resized_path)

                return resized_path

        except Exception as e:
            logger.error(
                "Error resizing image",
                error=str(e),
                exc_info=True
            )

            raise ImageProcessingError(
                f"Error resizing image: {str(e)}"
            )

    def _convert_pdf_to_image(self, pdf_path: str) -> str:
        """
        Convert the first page of a PDF into a printer-ready image.

        Args:
            pdf_path: Path to the PDF file.

        Returns:
            Path to the generated PNG image.

        Raises:
            ImageProcessingError: If PDF conversion fails.
        """

        try:
            logger.info(
                "Converting PDF to image",
                pdf_path=pdf_path
            )

            #
            # Open PDF
            #
            pdf = fitz.open(pdf_path)

            if len(pdf) == 0:
                raise ValueError("PDF contains no pages")

            #
            # First page only
            #
            page = pdf[0]

            #
            # Render at 300 DPI for thermal clarity
            #
            pix = page.get_pixmap(
                dpi=300,
                colorspace=fitz.csGRAY
            )

            #
            # Convert to PIL image
            #
            image = Image.open(
                io.BytesIO(
                    pix.tobytes("png")
                )
            )

            #
            # Auto rotate portrait labels
            #
            if image.height > image.width:
                image = image.rotate(
                    90,
                    expand=True
                )

            #
            # Resize to Brother printable width
            #
            target_width = 696

            aspect_ratio = image.height / image.width

            target_height = int(
                target_width * aspect_ratio
            )

            image = image.resize(
                (target_width, target_height),
                Image.Resampling.LANCZOS
            )

            #
            # Monochrome for thermal optimization
            #
            image = image.convert("1")

            #
            # Save processed image
            #
            output_path = os.path.join(
                self.upload_folder,
                f"pdf_{uuid.uuid4().hex[:8]}.png"
            )

            image.save(output_path)

            logger.info(
                "PDF converted successfully",
                pdf_path=pdf_path,
                output_path=output_path
            )

            return output_path

        except Exception as e:
            logger.error(
                "Error converting PDF",
                pdf_path=pdf_path,
                error=str(e),
                exc_info=True
            )

            raise ImageProcessingError(
                f"Error converting PDF: {str(e)}"
            )

    def _apply_rotation(self, image_path: str, angle: int) -> str:
        """
        Apply rotation to an image.
        """

        try:
            with Image.open(image_path) as img:

                rotated_img = img.rotate(
                    -angle,
                    resample=Image.Resampling.LANCZOS,
                    expand=True
                )

                filename = os.path.basename(image_path)

                rotated_path = os.path.join(
                    self.upload_folder,
                    f"rotated_{filename}"
                )

                rotated_img.save(rotated_path)

                return rotated_path

        except Exception as e:
            logger.error(
                "Error rotating image",
                error=str(e),
                exc_info=True
            )

            raise ImageProcessingError(
                f"Error rotating image: {str(e)}"
            )

    def _send_to_printer(
        self,
        image_path: str,
        settings: Dict[str, Any]
    ) -> None:
        """
        Send an image to the printer.
        """

        try:
            printer_uri = settings.get("printer_uri")
            printer_model = settings.get("printer_model")
            label_size = settings.get("label_size")

            rotate = settings.get("rotate", 0)
            threshold = float(settings.get("threshold", 70.0))
            dither = settings.get("dither", False)
            compress = settings.get("compress", False)
            red = settings.get("red", False)

            if not printer_uri:
                raise ValueError("printer_uri is required")

            if not printer_model:
                raise ValueError("printer_model is required")

            if not label_size:
                raise ValueError("label_size is required")

            qlr = BrotherQLRaster(printer_model)
            qlr.exception_on_warning = True

            instructions = convert(
                qlr=qlr,
                images=[image_path],
                label=label_size,
                rotate=rotate,
                threshold=threshold,
                dither=dither,
                compress=compress,
                red=red,
            )

            backend = backend_factory(
                guess_backend(printer_uri)
            )["backend_class"](printer_uri)

            backend.write(instructions)
            backend.dispose()

            logger.info(
                "Print job sent to printer",
                printer_uri=printer_uri,
                printer_model=printer_model,
                label_size=label_size
            )

        except Exception as e:
            logger.error(
                "Error sending to printer",
                error=str(e),
                exc_info=True
            )

            raise PrinterError(
                f"Error sending to printer: {str(e)}"
            )


# Create singleton instance
printer_service = PrinterService()
