from abc import ABC, abstractmethod


class CameraProducer(ABC):
    @abstractmethod
    async def start(self) -> None:
        """
        Start camera acquisition.
        """
        pass

    @abstractmethod
    async def stop(self) -> None:
        """
        Stop camera acquisition.
        """
        pass