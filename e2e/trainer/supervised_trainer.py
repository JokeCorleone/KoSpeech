import time
import torch
import queue
import pandas as pd
from e2e.data_loader.data_loader import MultiDataLoader, AudioDataLoader
from e2e.modules.checkpoint import Checkpoint
from e2e.modules.error_rate import CharacterErrorRater
from e2e.modules.global_ import EOS_token, logger, id2char
from e2e.optim.lr_scheduler import ExponentialDecayLR


class SupervisedTrainer(object):
    """
    The SupervisedTrainer class helps in setting up training framework in a supervised setting.

    Args:
        optimizer (e2e.optim.optim.Optimizer): optimizer for training
        criterion (torch.nn.Module): loss function
        trainset_list (list): list of training datset
        validset (e2e.dataset.data_loader.SpectrogramDataset): validation dataset
        num_workers (int): number of using cpu cores
        device (torch.device): device - 'cuda' or 'cpu'
        print_every (int): number of timesteps to print result after
        save_result_every (int): number of timesteps to save result after
        checkpoint_every (int): number of timesteps to checkpoint after
    """
    train_step_result = {'loss': [], 'cer': []}
    train_dict = {'loss': [], 'cer': []}
    valid_dict = {'loss': [], 'cer': []}
    TRAIN_RESULT_PATH = "./data/train_result/train_result.csv"
    VALID_RESULT_PATH = "./data/train_result/eval_result.csv"
    TRAIN_STEP_RESULT_PATH = "./data/train_result/train_step_result.csv"

    def __init__(self, optimizer, criterion, trainset_list, validset, high_plateau_lr, low_plateau_lr,
                 exp_decay_period, num_workers, device, print_every, save_result_every, checkpoint_every):
        self.num_workers = num_workers
        self.optimizer = optimizer
        self.criterion = criterion
        self.trainset_list = trainset_list
        self.validset = validset
        self.high_plateau_lr = high_plateau_lr
        self.low_plateau_lr = low_plateau_lr
        self.exp_decay_period = exp_decay_period
        self.print_every = print_every
        self.save_result_every = save_result_every
        self.checkpoint_every = checkpoint_every
        self.device = device

    def train(self, model, batch_size, epoch_time_step, num_epochs, teacher_forcing_ratio=0.99, resume=False):
        """
        Run training for a given model.

        Args:
            model (torch.nn.Module): model to train
            batch_size (int): batch size for experiment
            epoch_time_step (int): number of time step for training
            num_epochs (int): number of epochs for training
            teacher_forcing_ratio (float): teaching forcing ratio (default 0.99)
            resume(bool, optional): resume training with the latest checkpoint, (default False)
        """
        start_epoch = 0
        prev_train_cer = 1.

        if resume:
            latest_checkpoint_path = Checkpoint.get_latest_checkpoint()
            resume_checkpoint = Checkpoint.load(latest_checkpoint_path)
            model = resume_checkpoint.model
            self.optimizer = resume_checkpoint.optimizer
            self.criterion = resume_checkpoint.criterion
            self.trainset_list = resume_checkpoint.trainset_list
            self.validset = resume_checkpoint.validset
            start_epoch = resume_checkpoint.epoch

        logger.info('start')
        train_begin_time = time.time()

        for epoch in range(start_epoch, num_epochs):
            train_queue = queue.Queue(self.num_workers << 1)
            for trainset in self.trainset_list:
                trainset.shuffle()

            # Training
            train_loader = MultiDataLoader(self.trainset_list, train_queue, batch_size, self.num_workers)
            train_loader.start()
            train_loss, train_cer = self.train_epoches(model, epoch, epoch_time_step, train_begin_time,
                                                       train_queue, teacher_forcing_ratio)
            train_loader.join()

            Checkpoint(model, self.optimizer, self.criterion, self.trainset_list, self.validset, epoch).save()
            logger.info('Epoch %d (Training) Loss %0.4f CER %0.4f' % (epoch, train_loss, train_cer))

            if prev_train_cer - train_cer < 0.01:
                scheduler = ExponentialDecayLR(self.optimizer.optimizer, self.optimizer.get_lr(),
                                               self.low_plateau_lr, self.exp_decay_period)
                self.optimizer.set_scheduler(scheduler, self.exp_decay_period)

            prev_train_cer = train_cer

            # Validation
            valid_queue = queue.Queue(self.num_workers << 1)
            valid_loader = AudioDataLoader(self.validset, valid_queue, batch_size, 0)
            valid_loader.start()

            valid_loss, valid_cer = self.validate(model, valid_queue)
            valid_loader.join()

            logger.info('Epoch %d (Validate) Loss %0.4f CER %0.4f' % (epoch, valid_loss, valid_cer))
            self._save_epoch_result(train_result=[self.train_dict, train_loss, train_cer],
                                    valid_result=[self.valid_dict, valid_loss, valid_cer])
            logger.info('Epoch %d Training result saved as a csv file complete !!' % epoch)

        return model

    def train_epoches(self, model, epoch, epoch_time_step, train_begin_time, queue, teacher_forcing_ratio):
        """
        Run training one epoch

        Args:
            model (torch.nn.Module): model to train
            epoch (int): number of current epoch
            epoch_time_step (int): total time step in one epoch
            train_begin_time (int): time of train begin
            queue (queue.Queue): training queue, containing input, targets, input_lengths, target_lengths
            teacher_forcing_ratio (float): teaching forcing ratio (default 0.99)

        Returns: loss, cer
            - **loss** (float): loss of current epoch
            - **cer** (float): character error rate of current epoch
        """
        rater = CharacterErrorRater(id2char, EOS_token)
        cer = 1.0
        epoch_loss_total = 0.
        total_num = 0
        timestep = 0

        model.train()
        begin_time = epoch_begin_time = time.time()

        while True:
            inputs, scripts, input_lengths, target_lengths = queue.get()

            if inputs.shape[0] == 0:
                # Empty feats means closing one loader
                self.num_workers -= 1
                logger.debug('left train_loader: %d' % self.num_workers)

                if self.num_workers == 0:
                    break
                else:
                    continue

            inputs = inputs.to(self.device)
            scripts = scripts.to(self.device)
            targets = scripts[:, 1:]

            model.module.flatten_parameters()
            output = model(inputs, input_lengths, scripts, teacher_forcing_ratio=teacher_forcing_ratio)

            logit = torch.stack(output, dim=1).to(self.device)
            hypothesis = logit.max(-1)[1]

            loss = self.criterion(logit.contiguous().view(-1, logit.size(-1)), targets.contiguous().view(-1))
            epoch_loss_total += loss.item()

            cer = rater.calc_error_rate(targets, hypothesis)
            total_num += int(input_lengths.sum())

            self.optimizer.zero_grad()
            loss.backward()
            self.optimizer.step(model, loss.item())

            timestep += 1
            torch.cuda.empty_cache()

            if timestep % self.print_every == 0:
                current_time = time.time()
                elapsed = current_time - begin_time
                epoch_elapsed = (current_time - epoch_begin_time) / 60.0
                train_elapsed = (current_time - train_begin_time) / 3600.0

                logger.info('timestep: {:4d}/{:4d}, loss: {:.4f}, cer: {:.2f}, elapsed: {:.2f}s {:.2f}m {:.2f}h'.format(
                    timestep,
                    epoch_time_step,
                    epoch_loss_total / total_num,
                    cer,
                    elapsed, epoch_elapsed, train_elapsed)
                )
                begin_time = time.time()

            if timestep % self.save_result_every == 0:
                self._save_step_result(self.train_step_result, epoch_loss_total / total_num, cer)

            if timestep % self.checkpoint_every == 0:
                Checkpoint(model, self.optimizer,  self.criterion, self.trainset_list, self.validset, epoch).save()

        Checkpoint(model, self.optimizer, self.criterion, self.trainset_list, self.validset, epoch).save()

        logger.info('train() completed')
        return epoch_loss_total / total_num, cer

    def validate(self, model, queue):
        """
        Run training one epoch

        Args:
            model (torch.nn.Module): model to train
            queue (queue.Queue): validation queue, containing input, targets, input_lengths, target_lengths

        Returns: loss, cer
            - **loss** (float): loss of validation
            - **cer** (float): character error rate of validation
        """
        total_loss = 0.
        total_num = 0
        total_dist = 0
        total_length = 0

        model.eval()
        logger.info('validate() start')

        with torch.no_grad():
            while True:
                inputs, scripts, input_lengths, script_lengths = queue.get()

                if inputs.shape[0] == 0:
                    break

                inputs = inputs.to(self.device)
                scripts = scripts.to(self.device)
                targets = scripts[:, 1:]

                model.module.flatten_parameters()
                output = model(inputs, input_lengths, teacher_forcing_ratio=0.0)

                logit = torch.stack(output, dim=1).to(self.device)
                hypothesis = logit.max(-1)[1]

                loss = self.criterion(logit.contiguous().view(-1, logit.size(-1)), targets.contiguous().view(-1))
                total_loss += loss.item()
                total_num += sum(input_lengths)

                dist, length = get_distance(targets, hypothesis, id2char, EOS_token)
                total_dist += dist
                total_length += length

        logger.info('validate() completed')
        return total_loss / total_num, total_dist / total_length

    def _save_epoch_result(self, train_result, valid_result):
        """ Save result of epoch """
        train_dict, train_loss, train_cer = train_result
        valid_dict, valid_loss, valid_cer = valid_result

        train_dict["loss"].append(train_loss)
        valid_dict["loss"].append(valid_loss)

        train_dict["cer"].append(train_cer)
        valid_dict["cer"].append(valid_cer)

        train_df = pd.DataFrame(train_dict)
        valid_df = pd.DataFrame(valid_dict)

        train_df.to_csv(self.TRAIN_RESULT_PATH, encoding="cp949", index=False)
        valid_df.to_csv(self.VALID_RESULT_PATH, encoding="cp949", index=False)

    def _save_step_result(self, train_step_result, loss, cer):
        """ Save result of --save_result_every step """
        train_step_result["loss"].append(loss)
        train_step_result["cer"].append(cer)

        train_step_df = pd.DataFrame(train_step_result)
        train_step_df.to_csv(self.TRAIN_STEP_RESULT_PATH, encoding="cp949", index=False)
